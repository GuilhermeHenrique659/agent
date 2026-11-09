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


def test_commit_paths_stages_only_given_paths_and_returns_none_without_changes(
    repo: GitRepo,
) -> None:
    assert repo.commit_paths(["docs"], "nothing") is None
    (repo.path / "docs").mkdir()
    (repo.path / "docs" / "a.md").write_text("a")
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "other.txt").write_text("o")
    before = repo.head()
    sha = repo.commit_paths(["docs", "missing-dir"], "docs: add a")
    assert sha is not None and sha == repo.head() != before
    assert repo.changed_files(sha) == ["docs/a.md"]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo.path, capture_output=True, text=True
    ).stdout
    assert " M README.md" in status and "?? other.txt" in status
    assert repo.commit_paths(["docs"], "again") is None


def test_delete_branch(repo: GitRepo) -> None:
    repo.create_branch("feature", "main")
    write_commit(repo, "f.txt", "f", "feature work")
    repo.checkout("main")
    with pytest.raises(GitError):
        repo.delete_branch("feature")  # unmerged: safe delete refuses
    repo.merge_ff("feature")
    repo.delete_branch("feature")
    assert not repo.branch_exists("feature")


def test_rebase_in_progress(repo: GitRepo) -> None:
    assert not repo.rebase_in_progress()
    conflicting_branches(repo)
    assert not repo.rebase("main").ok
    assert repo.rebase_in_progress()
    repo.rebase_abort()
    assert not repo.rebase_in_progress()


def test_tracked_files(repo: GitRepo) -> None:
    (repo.path / "src").mkdir()
    (repo.path / "src" / "a b.py").write_text("x")
    repo.commit_all("more")
    (repo.path / "untracked.txt").write_text("x")
    assert repo.tracked_files() == ["README.md", "src/a b.py"]


def test_churn_orders_by_commit_count_and_excludes_agent_dir(repo: GitRepo) -> None:
    write_commit(repo, "b.py", "1", "b1")
    write_commit(repo, "a.py", "1", "a1")
    write_commit(repo, "hot.py", "1", "h1")
    write_commit(repo, "hot.py", "2", "h2")
    write_commit(repo, "hot.py", "3", "h3")
    write_commit(repo, "gone.py", "1", "g1")
    write_commit(repo, "gone.py", "2", "g2")
    (repo.path / "gone.py").unlink()
    repo.commit_all("remove gone")
    (repo.path / ".agent").mkdir()
    for i in range(5):
        write_commit(repo, ".agent/notes.md", str(i), f"n{i}")
    assert repo.churn() == [("hot.py", 3), ("README.md", 1), ("a.py", 1), ("b.py", 1)]
    assert repo.churn(limit=2) == [("hot.py", 3), ("README.md", 1)]


def test_remotes(repo: GitRepo, tmp_path: Path) -> None:
    assert repo.remotes() == []
    git(repo.path, "remote", "add", "origin", str(tmp_path))
    git(repo.path, "remote", "add", "upstream", str(tmp_path))
    assert repo.remotes() == ["origin", "upstream"]


def test_commit_paths_on_unborn_repo(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path)
    repo.init("main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "a.txt").write_text("a")
    (tmp_path / "other.txt").write_text("o")
    assert repo.commit_paths([".agent", "CLAUDE.md"], "chore: first") is not None
    assert repo.tracked_files() == [".agent/a.txt"]
    assert repo.commit_paths([".agent"], "again") is None


def test_changed_paths_and_restore(repo: GitRepo) -> None:
    write_commit(repo, "a.txt", "a\n", "add a")
    assert repo.changed_paths() == []
    (repo.path / "a.txt").write_text("changed\n")
    (repo.path / "b.txt").write_text("new\n")
    (repo.path / "dir").mkdir()
    (repo.path / "dir" / "c.txt").write_text("c\n")
    (repo.path / "README.md").unlink()
    assert repo.changed_paths() == ["README.md", "a.txt", "b.txt", "dir/c.txt"]
    repo.restore(["a.txt", "b.txt", "README.md"])
    assert (repo.path / "a.txt").read_text() == "a\n"
    assert (repo.path / "README.md").read_text() == "hello\n"
    assert not (repo.path / "b.txt").exists()
    assert repo.changed_paths() == ["dir/c.txt"]
    repo.restore([])


def test_commits_since(repo: GitRepo) -> None:
    base = repo.head()
    assert repo.commits_since(base) == []
    first = write_commit(repo, "a.txt", "a\n", "test(T1): first")
    second = write_commit(repo, "b.txt", "b\n", "feat(T1): second")
    assert repo.commits_since(base) == [(first, "test(T1): first"), (second, "feat(T1): second")]


def test_diff_names_lists_added_modified_deleted_and_renamed_paths(repo: GitRepo) -> None:
    (repo.path / "gone.txt").write_text("gone\n")
    (repo.path / "old.txt").write_text("a long enough line\nto be seen as a rename\n")
    base = repo.commit_all("base")
    (repo.path / "gone.txt").unlink()
    (repo.path / "old.txt").rename(repo.path / "new.txt")
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "sub").mkdir()
    (repo.path / "sub" / "added.txt").write_text("x")
    repo.commit_all("change")
    assert repo.diff_names(base, repo.head()) == [
        "README.md",
        "gone.txt",
        "new.txt",
        "old.txt",
        "sub/added.txt",
    ]


def test_checkout_paths_restores_existing_and_removes_missing_paths(repo: GitRepo) -> None:
    base = repo.head()
    (repo.path / "README.md").write_text("changed\n")
    (repo.path / "extra.txt").write_text("x")
    (repo.path / "sub").mkdir()
    (repo.path / "sub" / "deep.txt").write_text("y")
    repo.commit_all("change")
    repo.checkout_paths(base, ["README.md", "extra.txt", "sub/deep.txt"])
    assert (repo.path / "README.md").read_text() == "hello\n"
    assert not (repo.path / "extra.txt").exists()
    assert not (repo.path / "sub" / "deep.txt").exists()
    assert set(repo.changed_paths()) == {"README.md", "extra.txt", "sub/deep.txt"}
    repo.reset_hard(repo.head())
    assert (repo.path / "README.md").read_text() == "changed\n"
    assert (repo.path / "extra.txt").exists()
    assert (repo.path / "sub" / "deep.txt").exists()


def test_added_lines_diff(repo: GitRepo) -> None:
    (repo.path / "a.py").write_text("one\ntwo\n")
    base = repo.commit_all("base")
    (repo.path / "a.py").write_text("one\nTWO\nthree\n")
    head = repo.commit_all("change")
    diff = repo.added_lines_diff(base, head)
    assert "+++ b/a.py" in diff
    assert "+TWO" in diff and "+three" in diff
    assert "-two" in diff
    assert "\n one" not in diff  # no context lines
