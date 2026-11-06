from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.gates.runner import GateReport, GateResult, run_gates
from autodev.gates.tdd import TddGuard, is_test_file
from autodev.pipeline import prompts
from autodev.pipeline.outputs import ImplementerOutput, ReviewOutput, TestWriterOutput
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource

_RESUMABLE = (StoryStatus.APPROVED, StoryStatus.IN_PROGRESS, StoryStatus.IN_REVIEW)
_STATE_DIRS = (".agent/backlog/", ".agent/runs/")
_LOG_DETAIL_CHARS = 2000


class RunError(Exception):
    pass


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
        self._check_state_ignored()
        if story.status in (StoryStatus.IN_QA, StoryStatus.DONE):
            return story
        if story.status not in _RESUMABLE:
            raise RunError(
                f"Story {story.id} is {story.status.value}; "
                "only approved, in_progress or in_review stories can be run"
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

    def _check_state_ignored(self) -> None:
        missing = [d for d in _STATE_DIRS if not self._repo.is_ignored(d)]
        if missing:
            listed = ", ".join(missing)
            raise RunError(
                f"Orchestrator state must not be versioned: {listed} not ignored by git. "
                "Add `backlog/` and `runs/` to .agent/.gitignore."
            )

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
                    await self._red(story, task)
                    await self._green(story, task)
                    task.complete()
                    self._save(story)
            await self._review_loop(story)
        except _Failed as failure:
            self._block(story, str(failure))

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
            if not self._repo.has_changes():
                feedback = "The test writer made no changes; tests must be written."
                continue
            sha = self._repo.commit_all(f"test({task.id}): {task.title}")
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

        await self._implement(
            task, build, f"({task.id}): {task.title}", first_prefix="feat", tdd=True
        )

    async def _implement(
        self,
        task: Task | None,
        build_prompt: Callable[[str | None], str],
        message: str,
        *,
        first_prefix: str,
        tdd: bool,
    ) -> None:
        """Run the implementer until the checks pass or the attempt budget is spent.

        Commits that fail only because tests/gates fail are kept (the next attempt fixes on top);
        commits that break the TDD rules, and agent errors, are reset.
        """
        feedback: str | None = None
        committed = False
        globs = self._config.tdd.test_globs
        what = task.id if task else "review"
        for _ in range(self._config.limits.max_attempts):
            before = self._repo.head()
            out = await self._agent(
                Role.IMPLEMENTER, build_prompt(feedback), ImplementerOutput, task
            )
            if isinstance(out, str):
                feedback = out
                self._repo.reset_hard(before)
                continue
            if not self._repo.has_changes():
                feedback = "The implementer made no changes."
                continue
            prefix = "fix" if committed else first_prefix
            sha = self._repo.commit_all(f"{prefix}{message}")
            if tdd:
                green = self._guard.check_green(sha)
                self._log_check("tdd_green", task, green)
                if not green.ok:
                    touched = [f for f in self._repo.changed_files(sha) if is_test_file(f, globs)]
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
            feedback = _gate_feedback(report)
        raise _Failed(f"{what}: implementation failed. {feedback}")

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

    # -- review ------------------------------------------------------------

    async def _review_loop(self, story: Story) -> None:
        base = self._config.git.main_branch
        max_attempts = self._config.limits.max_attempts
        while True:
            if story.status == StoryStatus.IN_PROGRESS:
                story.advance(StoryStatus.IN_REVIEW)
                self._save(story)
            prompt = prompts.build_reviewer_prompt(story, base, self._context_paths())
            out = await self._agent(Role.REVIEWER, prompt, ReviewOutput, None)
            if isinstance(out, str):
                raise _Failed(f"Review failed. {out}")
            self._log.append("review", ok=out.approved, detail=out.summary)
            if out.approved:
                story.advance(StoryStatus.IN_QA)
                self._save(story)
                return
            story.send_back(reason=out.summary, max_attempts=max_attempts)
            self._save(story)
            if story.status == StoryStatus.BLOCKED:
                self._log.append("story_blocked", detail=out.summary)
                return
            review = out

            def build(feedback: str | None, review: ReviewOutput = review) -> str:
                return prompts.build_review_fix_prompt(
                    story, base, self._context_paths(), review.summary, review.findings, feedback
                )

            await self._implement(
                None, build, "(review): address review findings", first_prefix="fix", tdd=False
            )


def _gate_feedback(report: GateReport) -> str:
    return "\n".join(f"Gate {r.name} failed:\n{r.output}" for r in report.failures)
