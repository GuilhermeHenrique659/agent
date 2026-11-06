import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(Exception):
    pass


@dataclass(frozen=True)
class RebaseResult:
    ok: bool
    conflicts: list[str]


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _run(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            env={**os.environ, **env} if env else None,
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc

    def is_repo(self) -> bool:
        proc = self._run("rev-parse", "--is-inside-work-tree", check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    def init(self, main_branch: str) -> None:
        self._run("init", "-b", main_branch)

    def current_branch(self) -> str:
        return self._run("symbolic-ref", "--short", "HEAD").stdout.strip()

    def branch_exists(self, name: str) -> bool:
        proc = self._run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        return proc.returncode == 0

    def create_branch(self, name: str, from_ref: str) -> None:
        self._run("checkout", "-b", name, from_ref)

    def checkout(self, ref: str) -> None:
        self._run("checkout", ref)

    def has_changes(self) -> bool:
        return bool(self._run("status", "--porcelain").stdout.strip())

    def commit_all(self, message: str) -> str:
        self._run("add", "-A")
        self._run("commit", "-m", message)
        return self.head()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").stdout.strip()

    def changed_files(self, commit: str) -> list[str]:
        out = self._run(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-z", commit
        ).stdout
        return sorted(f for f in out.split("\0") if f)

    def is_ignored(self, path: str) -> bool:
        proc = self._run("check-ignore", "-q", path, check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise GitError(f"git check-ignore {path} failed: {proc.stderr.strip()}")

    def reset_hard(self, ref: str) -> None:
        """Reset HEAD and the working tree to `ref` and delete untracked files (git clean -fd).

        Dropping untracked files removes whatever an agent created since `ref`.
        """
        self._run("reset", "--hard", ref)
        self._run("clean", "-fd")

    def rebase(self, onto: str) -> RebaseResult:
        proc = self._run("rebase", onto, check=False)
        return self._rebase_result(proc, f"rebase {onto}")

    def conflicted_files(self) -> list[str]:
        out = self._run("diff", "--name-only", "--diff-filter=U", "-z").stdout
        return sorted(f for f in out.split("\0") if f)

    def rebase_continue(self) -> RebaseResult:
        self._run("add", "-A")
        proc = self._run("rebase", "--continue", check=False, env={"GIT_EDITOR": "true"})
        return self._rebase_result(proc, "rebase --continue")

    def rebase_abort(self) -> None:
        self._run("rebase", "--abort")

    def merge_ff(self, branch: str) -> None:
        self._run("merge", "--ff-only", branch)

    def pull(self, remote: str, branch: str) -> None:
        self._run("pull", "--rebase", remote, branch)

    def push(self, remote: str, branch: str) -> None:
        self._run("push", remote, branch)

    def _rebase_result(self, proc: subprocess.CompletedProcess[str], what: str) -> RebaseResult:
        if proc.returncode == 0:
            return RebaseResult(True, [])
        conflicts = self.conflicted_files()
        if not conflicts:
            raise GitError(f"git {what} failed: {proc.stderr.strip()}")
        return RebaseResult(False, conflicts)
