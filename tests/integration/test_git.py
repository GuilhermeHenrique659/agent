import subprocess
from pathlib import Path

import pytest

from autodev.adapters.git import GitError, GitRepo
from tests.integration.conftest import git


def write_commit(repo: GitRepo, name: str, content: str, message: str) -> str:
    (repo.path / name).write_text(content)
    return repo.commit_all(message)


def test_init_and_current_branch(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path)
    assert not repo.is_repo()
    repo.init("trunk")
    assert repo.is_repo()
    assert repo.current_branch() == "trunk"


def test_create_branch_and_checkout(repo: GitRepo) -> None:
    assert not repo.branch_exists("story/S-1")
    repo.create_branch("story/S-1", "main")
    assert repo.branch_exists("story/S-1")
    assert repo.current_branch() == "story/S-1"
    repo.checkout("main")
    assert repo.current_branch() == "main"


def test_has_changes_and_commit_all_returns_sha(repo: GitRepo) -> None:
    assert not repo.has_changes()
    (repo.path / "new.txt").write_text("x")
    assert repo.has_changes()
    sha = repo.commit_all("add new")
    assert len(sha) == 40
    assert sha == repo.head()
    assert not repo.has_changes()


def test_commit_all_without_changes_raises(repo: GitRepo) -> None:
    with pytest.raises(GitError):
        repo.commit_all("nothing")


def test_changed_files_of_commit(repo: GitRepo) -> None:
    (repo.path / "sub").mkdir()
    (repo.path / "sub" / "b.txt").write_text("b")
    (repo.path / "a.txt").write_text("a")
    sha = repo.commit_all("two files")
    assert repo.changed_files(sha) == ["a.txt", "sub/b.txt"]
    first = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        cwd=repo.path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert repo.changed_files(first) == ["README.md"]


def test_rebase_without_conflict(repo: GitRepo) -> None:
    repo.create_branch("feature", "main")
    write_commit(repo, "f.txt", "f", "feature work")
    repo.checkout("main")
    write_commit(repo, "m.txt", "m", "main work")
    repo.checkout("feature")
    result = repo.rebase("main")
    assert result.ok
    assert result.conflicts == []
    assert (repo.path / "m.txt").exists()
    assert (repo.path / "f.txt").exists()


def conflicting_branches(repo: GitRepo) -> None:
    repo.create_branch("feature", "main")
    write_commit(repo, "README.md", "feature\n", "feature edit")
    repo.checkout("main")
    write_commit(repo, "README.md", "main\n", "main edit")
    repo.checkout("feature")


def test_rebase_with_conflict_reports_files_then_continue_after_resolution(
    repo: GitRepo,
) -> None:
    conflicting_branches(repo)
    result = repo.rebase("main")
    assert not result.ok
    assert result.conflicts == ["README.md"]
    assert repo.conflicted_files() == ["README.md"]
    (repo.path / "README.md").write_text("resolved\n")
    done = repo.rebase_continue()
    assert done.ok
    assert done.conflicts == []
    assert repo.current_branch() == "feature"
    assert (repo.path / "README.md").read_text() == "resolved\n"


def test_rebase_abort_restores_branch(repo: GitRepo) -> None:
    conflicting_branches(repo)
    before = repo.head()
    assert not repo.rebase("main").ok
    repo.rebase_abort()
    assert repo.current_branch() == "feature"
    assert repo.head() == before
    assert repo.conflicted_files() == []


def test_merge_ff_and_non_ff_raises(repo: GitRepo) -> None:
    repo.create_branch("feature", "main")
    sha = write_commit(repo, "f.txt", "f", "feature work")
    repo.checkout("main")
    repo.merge_ff("feature")
    assert repo.head() == sha

    repo.create_branch("other", "main")
    write_commit(repo, "o.txt", "o", "other work")
    repo.checkout("main")
    write_commit(repo, "m.txt", "m", "main work")
    with pytest.raises(GitError):
        repo.merge_ff("other")


def test_push_and_pull_with_local_bare_remote(repo: GitRepo, tmp_path: Path) -> None:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True)
    git(repo.path, "remote", "add", "origin", str(bare))
    repo.push("origin", "main")

    clone_path = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone_path)], check=True, capture_output=True)
    git(clone_path, "config", "user.name", "Other")
    git(clone_path, "config", "user.email", "other@example.com")
    clone = GitRepo(clone_path)
    sha = write_commit(clone, "c.txt", "c", "from clone")
    clone.push("origin", "main")

    repo.pull("origin", "main")
    assert repo.head() == sha
    assert (repo.path / "c.txt").exists()


def test_reset_hard_drops_commits_and_untracked_files(repo: GitRepo) -> None:
    base = repo.head()
    write_commit(repo, "a.txt", "a", "add a")
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "junk.txt").write_text("junk")
    (repo.path / "dir").mkdir()
    (repo.path / "dir" / "x.txt").write_text("x")
    repo.reset_hard(base)
    assert repo.head() == base
    assert not repo.has_changes()
    assert not (repo.path / "a.txt").exists()
    assert not (repo.path / "junk.txt").exists()
    assert not (repo.path / "dir").exists()
    assert (repo.path / "README.md").read_text() == "hello\n"


def test_is_ignored(repo: GitRepo) -> None:
    (repo.path / ".gitignore").write_text("state/\n")
    assert repo.is_ignored("state/")
    assert not repo.is_ignored("other/")
