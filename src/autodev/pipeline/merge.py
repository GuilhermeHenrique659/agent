from collections.abc import Awaitable, Callable
from pathlib import Path

from autodev.adapters.git import GitError, GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.story import Story
from autodev.gates.runner import GateReport
from autodev.pipeline import prompts
from autodev.pipeline.outputs import MergeOutput
from autodev.pipeline.run_log import RunLog

_START = "<<<<<<<"
_END = ">>>>>>>"

MergerCall = Callable[[str], Awaitable[MergeOutput | str]]
RunGates = Callable[[], GateReport]
# Runs the implementer until the gates pass; takes (prompt builder, commit title).
FixGates = Callable[[Callable[[str | None], str], str], Awaitable[None]]


class MergeFailed(Exception):
    """The merge could not be completed; the story must be blocked with this reason."""


def _has_markers(text: str) -> bool:
    return any(
        line.startswith((f"{_START} ", f"{_END} ")) or line.rstrip() in (_START, _END)
        for line in text.splitlines()
    )


def files_with_conflict_markers(root: Path, files: list[str]) -> list[str]:
    """Files that still contain conflict start/end markers (missing files count as clean)."""
    found: list[str] = []
    for name in files:
        path = root / name
        if path.is_file() and _has_markers(path.read_text(errors="replace")):
            found.append(name)
    return found


class Merger:
    """Rebase the story branch onto main, re-run the gates, fast-forward main, push."""

    def __init__(
        self,
        config: ProjectConfig,
        root: Path,
        repo: GitRepo,
        log: RunLog,
        context: Callable[[], list[Path]],
        *,
        run_merger: MergerCall,
        run_gates: RunGates,
        fix_gates: FixGates,
    ) -> None:
        self._config = config
        self._root = root
        self._repo = repo
        self._log = log
        self._context = context
        self._run_merger = run_merger
        self._run_gates = run_gates
        self._fix_gates = fix_gates

    async def merge(self, story: Story) -> None:
        """Merge `story/<id>` into main; raises MergeFailed (repo back on the story branch)."""
        branch = f"story/{story.id}"
        try:
            await self._merge(story, branch)
        except (MergeFailed, GitError) as exc:
            self._restore(branch)
            if isinstance(exc, GitError):
                raise MergeFailed(str(exc)) from exc
            raise

    def _restore(self, branch: str) -> None:
        try:
            if self._repo.rebase_in_progress():
                self._repo.rebase_abort()
            self._repo.checkout(branch)
        except GitError:
            pass

    async def _merge(self, story: Story, branch: str) -> None:
        remote = self._config.git.remote
        main = self._config.git.main_branch
        if remote:
            try:
                self._repo.checkout(main)
                self._repo.pull(remote, main)
                self._repo.checkout(branch)
            except GitError as exc:
                raise MergeFailed(f"Pull failed: {exc}") from exc
        result = self._repo.rebase(main)
        while not result.ok:
            await self._resolve_conflicts(story, result.conflicts)
            result = self._repo.rebase_continue()
        await self._make_gates_pass(story)
        self._repo.checkout(main)
        self._repo.merge_ff(branch)
        self._log.append("merged", detail=f"{branch} -> {main}")
        if remote:
            try:
                self._repo.push(remote, main)
            except GitError as exc:
                raise MergeFailed(
                    f"Merged locally but push failed: {exc}. Push manually, then `autodev resume`."
                ) from exc
            self._log.append("pushed", detail=f"{remote}/{main}")

    async def _resolve_conflicts(self, story: Story, conflicted: list[str]) -> None:
        self._log.append("merge_conflict", detail=", ".join(conflicted))
        feedback: str | None = None
        remaining = conflicted
        for _ in range(self._config.limits.max_attempts):
            out = await self._run_merger(prompts.build_merge_prompt(story, conflicted, feedback))
            if isinstance(out, str):
                feedback = out
                continue
            remaining = files_with_conflict_markers(self._root, conflicted)
            if not remaining:
                self._log.append("merge_resolved", detail=", ".join(conflicted))
                return
            feedback = "Conflict markers remain in: " + ", ".join(remaining)
        raise MergeFailed(f"Merge conflict could not be resolved: {', '.join(remaining)}")

    async def _make_gates_pass(self, story: Story) -> None:
        report = self._run_gates()
        if report.ok:
            return
        initial = prompts.gate_feedback(report)
        base = self._config.git.main_branch

        def build(feedback: str | None) -> str:
            return prompts.build_merge_fix_prompt(story, base, self._context(), feedback or initial)

        await self._fix_gates(build, "make gates pass after rebase")
