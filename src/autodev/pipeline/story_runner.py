from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from autodev.adapters.git import GitError, GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.gates.failure_files import gate_failure_files
from autodev.gates.runner import GateReport, GateResult, run_command, run_gates
from autodev.gates.tdd import TddGuard, is_test_file
from autodev.pipeline import prompts
from autodev.pipeline.commit_message import commit_message
from autodev.pipeline.merge import MergeFailed, Merger
from autodev.pipeline.outputs import (
    ImplementerOutput,
    MergeOutput,
    QaOutput,
    ReviewOutput,
    TestWriterOutput,
)
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
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
            for task in story.tasks:
                if task.status == TaskStatus.PENDING:
                    self._restart_task(task)
                    await self._red(story, task)
                    await self._green(story, task)
                    task.complete()
                    self._save(story)
            await self._quality_loop(story)
        except _Failed as failure:
            self._block(story, str(failure))

    def _restart_task(self, task: Task) -> None:
        """Drop the commits of an earlier, unfinished attempt at `task` so it restarts."""
        prefixes = tuple(f"{kind}({task.id}):" for kind in _RESTART_KINDS)
        for sha, subject in self._repo.commits_since(self._config.git.main_branch):
            if subject.startswith(prefixes):
                parent = self._repo.rev_parse(f"{sha}^")
                self._repo.reset_hard(parent)
                self._log.append("task_restarted", task=task.id, detail=parent)
                return

    def _format(self, task: Task | None, *, protect_non_tests: bool = False) -> None:
        """Run the configured formatter on the working tree before an agent commit.

        With `protect_non_tests`, non-test files the formatter changed on its own
        (the agent had not touched them) are restored, so it cannot break the test-only rules.
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
        if protect_non_tests:
            globs = self._config.tdd.test_globs
            by_formatter = [
                p
                for p in self._repo.changed_paths()
                if p not in touched_before and not is_test_file(p, globs)
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

    async def _red(self, story: Story, task: Task) -> None:
        feedback: str | None = None
        base = self._config.git.main_branch
        for _ in range(self._config.limits.max_attempts):
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
            result = self._guard.check_red(sha)
            self._log_check("tdd_red", task, result)
            if result.ok:
                return
            feedback = f"Red check failed:\n{result.output}"
            self._repo.reset_hard(before)
        raise _Failed(f"Task {task.id}: red phase failed. {feedback}")

    # -- GREEN / review fixes ----------------------------------------------

    async def _green(self, story: Story, task: Task) -> None:
        base = self._config.git.main_branch

        def build(feedback: str | None) -> str:
            return prompts.build_implementer_prompt(
                story, task, base, self._context_paths(), feedback
            )

        def build_test_fix(gate_output: str | None) -> str:
            return prompts.build_test_fix_prompt(
                story, task, base, self._context_paths(), gate_output
            )

        await self._implement(
            task,
            build,
            task.id,
            task.title,
            first_prefix="feat",
            tdd=True,
            build_test_fix=build_test_fix,
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
    ) -> None:
        """Run the implementer until the checks pass or the attempt budget is spent.

        Commits that fail only because tests/gates fail are kept (the next attempt fixes on top);
        commits that break the TDD rules, and agent errors, are reset.
        With `build_test_fix`, a gate failure that points only at test files makes the next
        attempt a test writer turn that fixes those files (the implementer may not touch them).
        """
        feedback: str | None = None
        committed = False
        test_fix_next = False
        globs = self._config.tdd.test_globs
        what = task.id if task else label
        for _ in range(self._config.limits.max_attempts):
            before = self._repo.head()
            test_fix = test_fix_next and task is not None and build_test_fix is not None
            test_fix_next = False
            if test_fix and task is not None and build_test_fix is not None:
                out: ImplementerOutput | TestWriterOutput | str = await self._agent(
                    Role.TEST_WRITER, build_test_fix(feedback), TestWriterOutput, task
                )
            else:
                out = await self._agent(
                    Role.IMPLEMENTER, build_prompt(feedback), ImplementerOutput, task
                )
            if isinstance(out, str):
                feedback = out
                self._repo.reset_hard(before)
                test_fix_next = test_fix
                continue
            if self._repo.has_changes():
                self._format(task, protect_non_tests=test_fix)
            no_change = not self._repo.has_changes()
            if no_change and not committed:
                feedback = "The implementer made no changes."
                continue
            if no_change:
                # An earlier attempt's commit was kept (its TDD rules were already checked);
                # the gates decide whether the unchanged tree is now fine.
                report = run_gates(self._config, self._root)
                self._log_report(task, report)
                if report.ok:
                    return
                feedback = (
                    "You made no changes, but the gates still fail:\n"
                    + prompts.gate_feedback(report)
                )
                test_fix_next = tdd and self._only_test_files_failed(report)
                continue
            if test_fix and task is not None:
                others = [p for p in self._repo.changed_paths() if not is_test_file(p, globs)]
                if others:
                    self._repo.reset_hard(before)
                    listed = "\n".join(others)
                    feedback = (
                        f"A test fix may only touch test files; non-test files changed:\n{listed}"
                    )
                    self._log.append("test_fix", task=task.id, ok=False, detail=feedback)
                    test_fix_next = True
                    continue
                self._repo.commit_all(commit_message("test", task.id, "fix test lint"))
                tested = self._run_tests()
                self._log.append(
                    "test_fix",
                    task=task.id,
                    ok=tested.ok,
                    detail=tested.output[-_LOG_DETAIL_CHARS:],
                )
                if not tested.ok:
                    feedback = f"Tests failed after your change:\n{tested.output}"
                    continue
            else:
                prefix = "fix" if committed else first_prefix
                sha = self._repo.commit_all(commit_message(prefix, scope, title))
                if tdd:
                    green = self._guard.check_green(sha)
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
                        continue
            committed = True
            report = run_gates(self._config, self._root)
            self._log_report(task, report)
            if report.ok:
                return
            feedback = prompts.gate_feedback(report)
            test_fix_next = tdd and self._only_test_files_failed(report)
        raise _Failed(f"{what}: implementation failed. {feedback}")

    def _only_test_files_failed(self, report: GateReport) -> bool:
        tests, others = gate_failure_files(report, self._root, self._config.tdd.test_globs)
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
                prompt = prompts.build_reviewer_prompt(story, base, self._context_paths())
                out = await self._agent(Role.REVIEWER, prompt, ReviewOutput, None)
                if isinstance(out, str):
                    raise _Failed(f"Review failed. {out}")
                self._log.append("review", ok=out.approved, detail=out.summary)
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


def _is_blocked(story: Story) -> bool:
    return story.status == StoryStatus.BLOCKED
