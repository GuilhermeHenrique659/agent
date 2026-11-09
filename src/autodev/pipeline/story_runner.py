import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from autodev.adapters.git import GitError, GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.gates.failure_files import gate_failure_files
from autodev.gates.forbidden import forbidden_additions
from autodev.gates.junit import TestOutcome, run_tests_with_report
from autodev.gates.runner import GateReport, GateResult, run_command, run_gates
from autodev.gates.tdd import TddGuard, is_test_file
from autodev.pipeline import prompts
from autodev.pipeline.commit_message import commit_message
from autodev.pipeline.merge import MergeFailed, Merger
from autodev.pipeline.outputs import (
    CriterionCoverage,
    Finding,
    ImplementerOutput,
    MergeOutput,
    QaOutput,
    ReviewOutput,
    ScaffoldOutput,
    TestWriterOutput,
)
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.test_changes import diff_of_test_files, truncate_diff
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource

_RESUMABLE = (
    StoryStatus.APPROVED,
    StoryStatus.IN_PROGRESS,
    StoryStatus.IN_REVIEW,
    StoryStatus.IN_QA,
)
_STATE_DIRS = (".agent/backlog/", ".agent/runs/")
_LOG_DETAIL_CHARS = 2000
_TITLE_CHARS = 60
_RESTART_KINDS = ("test", "feat", "fix")
CONFIRM_TEST_CHANGES = "Confirm the test changes after red against the acceptance criteria"


class RunError(Exception):
    pass


def check_state_ignored(repo: GitRepo) -> None:
    """Orchestrator state (backlog, runs) must stay out of git."""
    missing = [d for d in _STATE_DIRS if not repo.is_ignored(d)]
    if missing:
        listed = ", ".join(missing)
        raise RunError(
            f"Orchestrator state must not be versioned: {listed} not ignored by git. "
            "Add `backlog/` and `runs/` to .agent/.gitignore."
        )


def check_junit_ignored(config: ProjectConfig, repo: GitRepo) -> None:
    """The JUnit report is rewritten by every test run; it must not be versioned."""
    report = config.tdd.junit_report
    if report is not None and not repo.is_ignored(report):
        raise RunError(
            "tdd.junit_report must point to a git-ignored path, e.g. .agent/runs/junit.xml"
        )


@dataclass(frozen=True)
class _TestTurn:
    """The next attempt is a test writer turn: a lint fix (no `defect`) or a test defect fix."""

    defect: str | None = None


@dataclass
class _Budget:
    """Red attempts left for a task; test writer and scaffold attempts spend it."""

    left: int


@dataclass(frozen=True)
class _RedResult:
    """What the red phase recorded for a task (`new_tests` is empty without a JUnit report)."""

    new_tests: frozenset[str]
    base_sha: str  # the scaffold commit if there is one, else the red commit
    pre_red: str  # HEAD before the test writer's commit


class _Failed(Exception):
    """An attempt budget was exhausted; carries the last failure."""


class StoryRunner:
    def __init__(
        self,
        config: ProjectConfig,
        project_root: Path,
        runtime: AgentRuntime,
        repo: GitRepo,
        store: StoryStore,
        context: ContextSource,
        log_factory: Callable[[str], RunLog],
    ) -> None:
        self._config = config
        self._root = project_root
        self._runtime = runtime
        self._repo = repo
        self._store = store
        self._context = context
        self._log_factory = log_factory
        self._guard = TddGuard(config, repo)
        self._log: RunLog

    async def run(self, story_id: str) -> Story:
        try:
            story = self._store.get(story_id)
        except KeyError as exc:
            raise RunError(f"Story {story_id} not found") from exc
        check_state_ignored(self._repo)
        check_junit_ignored(self._config, self._repo)
        if story.status == StoryStatus.DONE:
            return story
        if story.status not in _RESUMABLE:
            raise RunError(
                f"Story {story.id} is {story.status.value}; "
                "only approved, in_progress, in_review or in_qa stories can be run"
            )
        if self._repo.has_changes():
            raise RunError("Working tree has uncommitted changes; commit or discard them first")
        self._log = self._log_factory(story.id)
        self._log.append("run_started", detail=story.status.value)
        try:
            await self._run(story)
        finally:
            self._log.append("run_finished", detail=story.status.value)
        return story

    def _save(self, story: Story) -> None:
        self._store.save(story)

    def _block(self, story: Story, reason: str) -> None:
        story.block(reason)
        self._save(story)
        self._log.append("story_blocked", detail=reason)

    async def _run(self, story: Story) -> None:
        branch = f"story/{story.id}"
        if self._repo.branch_exists(branch):
            self._repo.checkout(branch)
        else:
            self._repo.create_branch(branch, self._config.git.main_branch)
        if story.status == StoryStatus.APPROVED:
            story.advance(StoryStatus.IN_PROGRESS)
            self._save(story)
        try:
            if self._config.tdd.junit_report is None:
                self._log.append(
                    "junit_report_missing",
                    detail="tdd.junit_report is not set: red/green are checked per suite only",
                )
            for task in story.tasks:
                if task.status == TaskStatus.PENDING:
                    await self._run_task(story, task)
                    task.complete()
                    self._save(story)
            await self._quality_loop(story)
        except _Failed as failure:
            self._block(story, str(failure))

    async def _run_task(self, story: Story, task: Task) -> None:
        self._restart_task(task)
        budget = _Budget(self._config.limits.max_attempts)
        baseline = self._baseline(task)
        feedback: str | None = None
        while True:
            red = await self._red(story, task, budget, baseline, feedback)
            self._save(story)
            await self._green(story, task, red)
            if baseline is None:
                return
            offenders = self._final_recheck(task, red, baseline)
            if not offenders:
                return
            listed = "\n".join(f"- {test}" for test in offenders)
            self._log.append("red_recheck_failed", task=task.id, ok=False, detail=listed)
            self._repo.reset_hard(red.pre_red)
            if budget.left <= 0:
                raise _Failed(
                    f"Task {task.id}: red re-check failed, these tests pass without the "
                    f"implementation:\n{listed}"
                )
            feedback = (
                "These tests pass even without the implementation, so they do not depend on the "
                "implementation of the task. Rewrite them to assert the behaviour of the task "
                f"so that they fail without it:\n{listed}"
            )

    def _baseline(self, task: Task) -> dict[str, TestOutcome] | None:
        """Outcomes of the suite at HEAD, before the task's tests (None without a JUnit report)."""
        if self._config.tdd.junit_report is None:
            return None
        result, outcomes = run_tests_with_report(self._config, self._root)
        if outcomes is None:
            raise _Failed(
                f"Task {task.id}: could not read the baseline test results. {result.output}"
            )
        return outcomes

    def _restart_task(self, task: Task) -> None:
        """Drop the commits of an earlier, unfinished attempt at `task` so it restarts."""
        prefixes = tuple(f"{kind}({task.id}):" for kind in _RESTART_KINDS)
        for sha, subject in self._repo.commits_since(self._config.git.main_branch):
            if subject.startswith(prefixes):
                parent = self._repo.rev_parse(f"{sha}^")
                self._repo.reset_hard(parent)
                self._log.append("task_restarted", task=task.id, detail=parent)
                return

    def _format(
        self, task: Task | None, *, protect_non_tests: bool = False, protect_tests: bool = False
    ) -> None:
        """Run the configured formatter on the working tree before an agent commit.

        With `protect_non_tests`, non-test files the formatter changed on its own
        (the agent had not touched them) are restored, so it cannot break the test-only rules.
        `protect_tests` does the same for test files (scaffold turns may not touch them).
        """
        command = self._config.commands.format
        if command is None:
            return
        touched_before = set(self._repo.changed_paths())
        code, output = run_command(command, self._root, self._config.limits.command_timeout_s)
        self._log.append(
            "format",
            task=task.id if task else None,
            ok=code == 0,
            detail=output[-_LOG_DETAIL_CHARS:],
        )
        if protect_non_tests or protect_tests:
            globs = self._config.tdd.test_globs
            by_formatter = [
                p
                for p in self._repo.changed_paths()
                if p not in touched_before and is_test_file(p, globs) == protect_tests
            ]
            if by_formatter:
                self._repo.restore(by_formatter)

    # -- agent calls -------------------------------------------------------

    async def _agent[T: BaseModel](
        self, role: Role, prompt: str, output_model: type[T], task: Task | None
    ) -> T | str:
        """Run an agent; returns its output, or an error message."""
        profile = self._config.agents.profile_for(role)
        instructions = prompts.load_instructions(role, profile, self._root / ".agent")
        request = AgentRequest(role, profile, instructions, prompt, self._root, output_model)
        task_id = task.id if task else None
        try:
            outcome = await self._runtime.run(request)
        except AgentError as exc:
            self._log.append("agent_run", role=role.value, task=task_id, ok=False, detail=str(exc))
            return f"Agent error: {exc}"
        summary = getattr(outcome.output, "summary", None)
        self._log.append(
            "agent_run",
            role=role.value,
            task=task_id,
            ok=True,
            cost_usd=outcome.cost_usd,
            turns=outcome.turns,
            detail=summary,
        )
        return outcome.output

    def _log_check(self, event: str, task: Task | None, result: GateResult) -> None:
        self._log.append(
            event,
            task=task.id if task else None,
            ok=result.ok,
            detail=f"{result.name}: {result.output[-_LOG_DETAIL_CHARS:]}",
        )

    def _context_paths(self) -> list[Path]:
        return self._context.paths()

    # -- RED ---------------------------------------------------------------

    async def _red(
        self,
        story: Story,
        task: Task,
        budget: _Budget,
        baseline: dict[str, TestOutcome] | None,
        feedback: str | None,
    ) -> _RedResult:
        """Write the failing tests. With a JUnit report (`baseline`) each new test is checked."""
        base = self._config.git.main_branch
        while budget.left > 0:
            budget.left -= 1
            before = self._repo.head()
            prompt = prompts.build_test_writer_prompt(
                story, task, base, self._context_paths(), feedback
            )
            out = await self._agent(Role.TEST_WRITER, prompt, TestWriterOutput, task)
            if isinstance(out, str):
                feedback = out
                self._repo.reset_hard(before)
                continue
            if self._repo.has_changes():
                self._format(task, protect_non_tests=True)
            if not self._repo.has_changes():
                feedback = "The test writer made no changes; tests must be written."
                continue
            sha = self._repo.commit_all(commit_message("test", task.id, task.title))
            if baseline is None:
                result = self._guard.check_red(sha)
                self._log_check("tdd_red", task, result)
                if result.ok:
                    task.coverage = _coverage_dict(out.coverage)
                    return _RedResult(frozenset(), sha, before)
                feedback = f"Red check failed:\n{result.output}"
            else:
                red, problem = await self._red_by_test(
                    story, task, sha, before, baseline, budget, out.coverage
                )
                if red is not None:
                    task.coverage = _coverage_dict(out.coverage)
                    return red
                feedback = problem
            self._repo.reset_hard(before)
        raise _Failed(f"Task {task.id}: red phase failed. {feedback}")

    async def _red_by_test(
        self,
        story: Story,
        task: Task,
        red_sha: str,
        pre_red: str,
        baseline: dict[str, TestOutcome],
        budget: _Budget,
        coverage: list[CriterionCoverage],
    ) -> tuple[_RedResult | None, str]:
        """Check the red commit test by test; returns the record, or the feedback for a retry."""
        files = self._guard.check_red_files(red_sha)
        self._log_check("tdd_red", task, files)
        if not files.ok:
            return None, f"Red check failed:\n{files.output}"
        result, outcomes = run_tests_with_report(self._config, self._root)
        if outcomes is None:
            return None, f"Red check failed:\n{result.output}"
        base_sha = red_sha
        if _needs_scaffold(result, outcomes, baseline):
            scaffolded = await self._scaffold(story, task, red_sha, result.output, budget)
            if isinstance(scaffolded, str):
                return None, scaffolded
            base_sha, result, outcomes = scaffolded
        new = {test: outcome for test, outcome in outcomes.items() if test not in baseline}
        problem = self._red_problem(task, new, result)
        detail = "\n".join(f"{test}: {outcome.value}" for test, outcome in sorted(new.items()))
        self._log.append("red_by_test", task=task.id, ok=problem is None, detail=detail)
        if problem is not None:
            return None, problem
        if problem := self._coverage_problem(story, task, coverage, set(new)):
            return None, problem
        return _RedResult(frozenset(new), base_sha, pre_red), ""

    def _coverage_problem(
        self, story: Story, task: Task, coverage: list[CriterionCoverage], new: set[str]
    ) -> str | None:
        """Every mapped criterion must exist and every mapped test must be one of the new tests."""
        total = len(story.acceptance_criteria)
        bad: list[str] = []
        for entry in coverage:
            if not 1 <= entry.criterion <= total:
                bad.append(
                    f"- criterion {entry.criterion} is out of range "
                    f"(the story has {total} acceptance criteria, numbered from 1)"
                )
            for name in entry.tests:
                if not any(test == name or test.endswith(f"::{name}") for test in new):
                    bad.append(
                        f"- criterion {entry.criterion}: test `{name}` is none of the new tests"
                    )
        self._log.append("red_coverage", task=task.id, ok=not bad, detail="\n".join(bad) or None)
        if not bad:
            return None
        listed = "\n".join(bad)
        known = "\n".join(f"- {t}" for t in sorted(new))
        return (
            "Red check failed: the coverage you reported is invalid. Map each test you wrote to "
            "the acceptance criteria it proves (1-based numbers) using the names of the test "
            f"functions you added:\n{listed}\n\nThe new tests are:\n{known}"
        )

    def _red_problem(
        self, task: Task, new: dict[str, TestOutcome], result: GateResult
    ) -> str | None:
        if not new:
            return (
                "Red check failed: the tests added no new test; the report has no test that was "
                f"not already in the suite.\n{result.output}"
            )
        vacuous = sorted(t for t, o in new.items() if o == TestOutcome.PASSED)
        if vacuous:
            listed = "\n".join(f"- {t}" for t in vacuous)
            self._log.append("red_vacuous", task=task.id, ok=False, detail=listed)
            return (
                "Red check failed: these tests pass against code with no behaviour; they must "
                f"assert the behaviour of the task:\n{listed}"
            )
        unusable = sorted(f"- {t}: {o.value}" for t, o in new.items() if o != TestOutcome.FAILED)
        if unusable:
            listed = "\n".join(unusable)
            return (
                "Red check failed: every new test must fail by an assertion, but these ended "
                f"otherwise:\n{listed}\n{result.output}"
            )
        return None

    async def _scaffold(
        self,
        story: Story,
        task: Task,
        red_sha: str,
        failure_output: str,
        budget: _Budget,
    ) -> tuple[str, GateResult, dict[str, TestOutcome]] | str:
        """Scaffold turn: declarations only, so that new tests can run and fail by assertion.

        Returns (scaffold commit, test result, outcomes), or feedback for the test writer.
        Every attempt spends the red budget; an exhausted budget fails the task.
        """
        base = self._config.git.main_branch
        globs = self._config.tdd.test_globs
        feedback: str | None = None
        while budget.left > 0:
            budget.left -= 1
            shown = failure_output if feedback is None else f"{feedback}\n\n{failure_output}"
            prompt = prompts.build_scaffold_prompt(story, task, base, self._context_paths(), shown)
            out = await self._agent(Role.IMPLEMENTER, prompt, ScaffoldOutput, task)
            if isinstance(out, str):
                feedback = out
            elif tests := [p for p in self._repo.changed_paths() if is_test_file(p, globs)]:
                listed = "\n".join(tests)
                feedback = f"A scaffold may not touch test files; test files changed:\n{listed}"
            else:
                if self._repo.has_changes():
                    self._format(task, protect_tests=True)
                if self._repo.has_changes():
                    sha = self._repo.commit_all(commit_message("chore", task.id, "scaffold"))
                    result, outcomes = run_tests_with_report(self._config, self._root)
                    self._log.append("scaffold", task=task.id, ok=True, detail=out.summary)
                    if outcomes is None:
                        return f"Red check failed after the scaffold:\n{result.output}"
                    return sha, result, outcomes
                feedback = "The scaffold made no changes; the declarations must be created."
            self._log.append("scaffold", task=task.id, ok=False, detail=feedback)
            self._repo.reset_hard(red_sha)
        raise _Failed(f"Task {task.id}: red phase failed. Scaffold: {feedback}")

    def _final_recheck(
        self, task: Task, red: _RedResult, baseline: dict[str, TestOutcome]
    ) -> list[str]:
        """Tests of the task that do not fail with production code back at the red state."""
        head = self._repo.head()
        globs = self._config.tdd.test_globs
        production = [
            p for p in self._repo.diff_names(red.base_sha, head) if not is_test_file(p, globs)
        ]
        try:
            self._repo.checkout_paths(red.base_sha, production)
            result, outcomes = run_tests_with_report(self._config, self._root)
        finally:
            self._repo.reset_hard(head)
        if outcomes is None:
            raise _Failed(
                f"Task {task.id}: red re-check could not read the test results. {result.output}"
            )
        offenders = sorted(
            test
            for test, outcome in outcomes.items()
            if test not in baseline and outcome not in (TestOutcome.FAILED, TestOutcome.ERROR)
        )
        self._log.append(
            "red_recheck", task=task.id, ok=not offenders, detail="\n".join(offenders) or None
        )
        return offenders

    # -- GREEN / review fixes ----------------------------------------------

    async def _green(self, story: Story, task: Task, red: _RedResult) -> None:
        base = self._config.git.main_branch

        def build(feedback: str | None) -> str:
            return prompts.build_implementer_prompt(
                story, task, base, self._context_paths(), feedback
            )

        def build_test_fix(gate_output: str | None) -> str:
            return prompts.build_test_fix_prompt(
                story, task, base, self._context_paths(), gate_output
            )

        def build_test_defect(defect: str, failure_output: str | None) -> str:
            return prompts.build_test_defect_prompt(
                story, task, base, self._context_paths(), defect, failure_output
            )

        await self._implement(
            task,
            build,
            task.id,
            task.title,
            first_prefix="feat",
            tdd=True,
            build_test_fix=build_test_fix,
            build_test_defect=build_test_defect,
            new_tests=red.new_tests,
            start_sha=red.pre_red,
        )

    async def _implement(
        self,
        task: Task | None,
        build_prompt: Callable[[str | None], str],
        scope: str,
        title: str,
        *,
        first_prefix: str,
        tdd: bool,
        label: str = "review",
        build_test_fix: Callable[[str | None], str] | None = None,
        build_test_defect: Callable[[str, str | None], str] | None = None,
        new_tests: frozenset[str] = frozenset(),
        start_sha: str | None = None,
    ) -> None:
        """Run the implementer until the checks pass or the attempt budget is spent.

        Commits that fail only because tests/gates fail are kept (the next attempt fixes on top);
        commits that break the TDD rules, and agent errors, are reset.
        With `build_test_fix`, a gate failure that points only at test files makes the next
        attempt a test writer turn that fixes those files (the implementer may not touch them).
        With `build_test_defect`, an implementer that reports a defect in the tests (while they
        still fail) makes the next attempt a test writer turn that fixes only that defect.
        When the gates pass, lines added to production files since `start_sha` (default: HEAD at
        the start of the loop) must not match `tdd.forbidden_patterns`; otherwise the attempt fails.
        """
        start = start_sha or self._repo.head()
        feedback: str | None = None
        committed = False
        next_turn: _TestTurn | None = None
        globs = self._config.tdd.test_globs
        what = task.id if task else label
        for _ in range(self._config.limits.max_attempts):
            before = self._repo.head()
            turn = next_turn
            next_turn = None
            if turn is not None and (
                task is None
                or (
                    build_test_defect is None if turn.defect is not None else build_test_fix is None
                )
            ):
                turn = None
            tests_before = self._count_tests() if turn and turn.defect is not None else 0
            declared: str | None = None
            if turn is not None and task is not None:
                if turn.defect is not None and build_test_defect is not None:
                    fix_prompt = build_test_defect(turn.defect, feedback)
                elif build_test_fix is not None:
                    fix_prompt = build_test_fix(feedback)
                else:
                    raise AssertionError("unreachable: test turn without prompt builder")
                out: ImplementerOutput | TestWriterOutput | str = await self._agent(
                    Role.TEST_WRITER, fix_prompt, TestWriterOutput, task
                )
            else:
                out = await self._agent(
                    Role.IMPLEMENTER, build_prompt(feedback), ImplementerOutput, task
                )
                if isinstance(out, ImplementerOutput) and tdd and build_test_defect is not None:
                    declared = out.test_defect or None
            if isinstance(out, str):
                feedback = out
                self._repo.reset_hard(before)
                next_turn = turn
                continue
            if self._repo.has_changes():
                self._format(task, protect_non_tests=turn is not None)
            no_change = not self._repo.has_changes()
            if no_change and turn is not None and turn.defect is not None:
                feedback = "The test writer made no changes; the reported defect must be fixed."
                next_turn = turn
                continue
            if no_change and not committed:
                feedback = "The implementer made no changes."
                if declared:
                    tested = self._run_tests()
                    if not tested.ok:
                        feedback = f"Tests failed:\n{tested.output}"
                        next_turn = _TestTurn(declared)
                continue
            if no_change:
                # An earlier attempt's commit was kept (its TDD rules were already checked);
                # the gates decide whether the unchanged tree is now fine.
                report = run_gates(self._config, self._root)
                self._log_report(task, report)
                if report.ok:
                    if not (forbidden := self._forbidden(task, start)):
                        return
                    feedback = forbidden
                    continue
                feedback = (
                    "You made no changes, but the gates still fail:\n"
                    + prompts.gate_feedback(report)
                )
                next_turn = self._next_turn(tdd, report, declared)
                continue
            if turn is not None and task is not None:
                event = "test_defect_fix" if turn.defect is not None else "test_fix"
                others = [p for p in self._repo.changed_paths() if not is_test_file(p, globs)]
                if others:
                    self._repo.reset_hard(before)
                    listed = "\n".join(others)
                    feedback = (
                        f"A test fix may only touch test files; non-test files changed:\n{listed}"
                    )
                    self._log.append(event, task=task.id, ok=False, detail=feedback)
                    next_turn = turn
                    continue
                if turn.defect is not None:
                    tests_after = self._count_tests()
                    if tests_after < tests_before:
                        self._repo.reset_hard(before)
                        feedback = (
                            "A test defect fix must not remove tests: there were "
                            f"{tests_before} test definitions before and {tests_after} after "
                            "your change."
                        )
                        self._log.append(event, task=task.id, ok=False, detail=feedback)
                        next_turn = turn
                        continue
                    subject = "fix test defect"
                else:
                    subject = "fix test lint"
                self._repo.commit_all(commit_message("test", task.id, subject))
                tested = self._run_tests()
                detail = turn.defect if tested.ok and turn.defect is not None else None
                self._log.append(
                    event,
                    task=task.id,
                    ok=tested.ok,
                    detail=detail or tested.output[-_LOG_DETAIL_CHARS:],
                )
                if not tested.ok:
                    feedback = f"Tests failed after your change:\n{tested.output}"
                    continue
            else:
                prefix = "fix" if committed else first_prefix
                sha = self._repo.commit_all(commit_message(prefix, scope, title))
                if tdd:
                    green = self._guard.check_green(sha, new_tests)
                    self._log_check("tdd_green", task, green)
                    if not green.ok:
                        touched = [
                            f for f in self._repo.changed_files(sha) if is_test_file(f, globs)
                        ]
                        if touched:
                            self._repo.reset_hard(before)
                            feedback = f"Green check failed:\n{green.output}"
                        else:
                            committed = True
                            feedback = f"Tests failed after your change:\n{green.output}"
                            if declared:
                                next_turn = _TestTurn(declared)
                        continue
            committed = True
            report = run_gates(self._config, self._root)
            self._log_report(task, report)
            if report.ok:
                if not (forbidden := self._forbidden(task, start)):
                    return
                feedback = forbidden
                continue
            feedback = prompts.gate_feedback(report)
            next_turn = self._next_turn(tdd, report, declared)
        raise _Failed(f"{what}: implementation failed. {feedback}")

    def _forbidden(self, task: Task | None, start: str) -> str | None:
        """Feedback listing forbidden lines added to production files since `start`, if any."""
        diff = self._repo.added_lines_diff(start, self._repo.head())
        found = forbidden_additions(
            diff, self._config.tdd.forbidden_patterns, self._config.tdd.test_globs
        )
        if not found:
            return None
        listed = "\n".join(f"- {line}" for line in found)
        self._log.append(
            "forbidden_patterns", task=task.id if task else None, ok=False, detail=listed
        )
        return (
            "These lines added to production files match a forbidden pattern "
            "(tdd.forbidden_patterns): placeholders, lint or type suppressions and TODO markers "
            f"must not be left behind. Remove them:\n{listed}"
        )

    def _next_turn(self, tdd: bool, report: GateReport, declared: str | None) -> "_TestTurn | None":
        if not tdd:
            return None
        if declared:
            return _TestTurn(declared)
        return _TestTurn() if self._only_test_files_failed(report) else None

    def _count_tests(self) -> int:
        """Test definitions (`tdd.test_name_pattern` matches) in the test files of the work tree."""
        regex = re.compile(self._config.tdd.test_name_pattern, re.MULTILINE)
        paths = set(self._repo.tracked_files()) | set(self._repo.changed_paths())
        total = 0
        for path in paths:
            file = self._root / path
            if is_test_file(path, self._config.tdd.test_globs) and file.is_file():
                total += sum(1 for _ in regex.finditer(file.read_text(errors="replace")))
        return total

    def _only_test_files_failed(self, report: GateReport) -> bool:
        tracked = set(self._repo.tracked_files())
        tests, others = gate_failure_files(report, self._root, self._config.tdd.test_globs, tracked)
        return bool(tests) and not others

    def _run_tests(self) -> GateResult:
        command = self._config.commands.test
        if command is None:
            return GateResult("test", True, "No command configured", skipped=True)
        code, output = run_command(command, self._root, self._config.limits.command_timeout_s)
        return GateResult("test", code == 0, output)

    def _log_report(self, task: Task | None, report: GateReport) -> None:
        detail = "\n".join(
            f"{r.name}: {'ok' if r.ok else 'failed'}" + ("" if r.ok else f"\n{r.output}")
            for r in report.results
        )
        self._log.append(
            "gate",
            task=task.id if task else None,
            ok=report.ok,
            detail=detail[-_LOG_DETAIL_CHARS:],
        )

    # -- review / QA / merge -----------------------------------------------

    async def _quality_loop(self, story: Story) -> None:
        base = self._config.git.main_branch
        max_attempts = self._config.limits.max_attempts
        while True:
            if story.status == StoryStatus.IN_PROGRESS:
                story.advance(StoryStatus.IN_REVIEW)
                self._save(story)
            if story.status == StoryStatus.IN_REVIEW:
                test_changes = self._test_changes_after_red()
                prompt = prompts.build_reviewer_prompt(
                    story, base, self._context_paths(), test_changes
                )
                out = await self._agent(Role.REVIEWER, prompt, ReviewOutput, None)
                if isinstance(out, str):
                    raise _Failed(f"Review failed. {out}")
                self._log.append("review", ok=out.approved, detail=out.summary)
                if out.approved:
                    out = self._enforce_review(story, out, bool(test_changes))
                if out.approved:
                    story.advance(StoryStatus.IN_QA)
                    self._save(story)
                else:
                    story.send_back(reason=out.summary, max_attempts=max_attempts)
                    self._save(story)
                    if _is_blocked(story):
                        self._log.append("story_blocked", detail=out.summary)
                        return
                    review = out

                    def build_review_fix(
                        feedback: str | None, review: ReviewOutput = review
                    ) -> str:
                        return prompts.build_review_fix_prompt(
                            story,
                            base,
                            self._context_paths(),
                            review.summary,
                            review.findings,
                            feedback,
                        )

                    await self._implement(
                        None,
                        build_review_fix,
                        "review",
                        "address review findings",
                        first_prefix="fix",
                        tdd=False,
                    )
                    continue
            prompt = prompts.build_qa_prompt(story, base, self._context_paths())
            qa = await self._agent(Role.QA, prompt, QaOutput, None)
            if isinstance(qa, str):
                raise _Failed(f"QA failed. {qa}")
            self._log.append("qa", ok=qa.approved, detail=qa.summary)
            if qa.approved:
                await self._merge(story)
                # only after a successful merge, so a resumed run can't duplicate them
                self._create_improvements(story, qa)
                return
            story.send_back(reason=qa.summary, max_attempts=max_attempts)
            self._save(story)
            if _is_blocked(story):
                self._log.append("story_blocked", detail=qa.summary)
                return

            def build_qa_fix(feedback: str | None, qa: QaOutput = qa) -> str:
                return prompts.build_qa_fix_prompt(story, base, self._context_paths(), qa, feedback)

            await self._implement(
                None,
                build_qa_fix,
                "qa",
                "address QA findings",
                first_prefix="fix",
                tdd=False,
                label="qa",
            )

    def _test_changes_after_red(self) -> str:
        """Diff of the test-file changes made after each task's red commit (empty if none)."""
        base = self._config.git.main_branch
        globs = self._config.tdd.test_globs
        red_commits: set[str] = set()
        seen: set[str] = set()
        commits = self._repo.commits_since(base)
        for sha, subject in commits:
            match = re.match(r"test\(([^)]+)\):", subject)
            if match and match.group(1) not in seen:
                seen.add(match.group(1))
                red_commits.add(sha)
        parts = [
            changes
            for sha, _ in commits
            if sha not in red_commits
            and (changes := diff_of_test_files(self._repo.added_lines_diff(f"{sha}^", sha), globs))
        ]
        return truncate_diff("\n".join(parts)) if parts else ""

    def _enforce_review(
        self, story: Story, out: ReviewOutput, has_test_changes: bool
    ) -> ReviewOutput:
        """An approval stands only if test changes were confirmed and criteria are covered."""
        findings: list[Finding] = []
        if has_test_changes and not out.test_changes_confirmed:
            findings.append(Finding(severity="blocking", message=CONFIRM_TEST_CHANGES))
        if self._config.tdd.junit_report is not None:
            reasons = {u.criterion for u in out.uncovered_criteria if u.reason.strip()}
            for index in prompts.uncovered_criteria(story):
                if index not in reasons:
                    findings.append(
                        Finding(
                            severity="blocking",
                            message=(
                                f"Acceptance criterion {index} "
                                f"({story.acceptance_criteria[index - 1]}) has no mapped tests "
                                "and no reason why it is not testable: add the missing test"
                            ),
                        )
                    )
        if not findings:
            return out
        detail = "; ".join(f.message for f in findings)
        self._log.append("review_overridden", ok=False, detail=detail)
        return ReviewOutput(
            approved=False,
            summary=f"{out.summary}\n\nApproval overridden by the orchestrator: {detail}",
            findings=[*out.findings, *findings],
            test_changes_confirmed=out.test_changes_confirmed,
            uncovered_criteria=out.uncovered_criteria,
        )

    def _create_improvements(self, story: Story, qa: QaOutput) -> None:
        for improvement in qa.improvements:
            draft = Story(
                self._store.next_id(),
                title=improvement.title[:_TITLE_CHARS],
                description=(
                    f"{improvement.description}\n\nSuggested by QA while delivering {story.id}."
                ),
            )
            self._save(draft)
            self._log.append("improvement_created", detail=draft.id)

    async def _merge(self, story: Story) -> None:
        async def run_merger(prompt: str) -> MergeOutput | str:
            return await self._agent(Role.MERGER, prompt, MergeOutput, None)

        def run_story_gates() -> GateReport:
            report = run_gates(self._config, self._root)
            self._log_report(None, report)
            return report

        async def fix_gates(build: Callable[[str | None], str], title: str) -> None:
            try:
                await self._implement(
                    None, build, "merge", title, first_prefix="fix", tdd=False, label="merge"
                )
            except _Failed as failure:
                raise MergeFailed(str(failure)) from failure

        merger = Merger(
            self._config,
            self._root,
            self._repo,
            self._log,
            self._context_paths,
            run_merger=run_merger,
            run_gates=run_story_gates,
            fix_gates=fix_gates,
        )
        try:
            await merger.merge(story)
        except MergeFailed as failure:
            raise _Failed(str(failure)) from failure
        story.advance(StoryStatus.DONE)
        self._save(story)
        self._log.append("story_done")
        try:
            self._repo.delete_branch(f"story/{story.id}")
        except GitError as exc:
            self._log.append("branch_delete_failed", detail=str(exc))


def _coverage_dict(coverage: list[CriterionCoverage]) -> dict[int, list[str]]:
    merged: dict[int, list[str]] = {}
    for entry in coverage:
        names = merged.setdefault(entry.criterion, [])
        names.extend(n for n in entry.tests if n not in names)
    return merged


def _needs_scaffold(
    result: GateResult, outcomes: dict[str, TestOutcome], baseline: dict[str, TestOutcome]
) -> bool:
    """Collection/import errors, or a failing run that produced no new test id at all."""
    if any(outcome == TestOutcome.ERROR for outcome in outcomes.values()):
        return True
    return not result.ok and all(test in baseline for test in outcomes)


def _is_blocked(story: Story) -> bool:
    return story.status == StoryStatus.BLOCKED
