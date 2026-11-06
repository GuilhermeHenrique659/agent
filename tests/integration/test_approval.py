import subprocess
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.story import Story, StoryStatus, Task
from autodev.pipeline.approval import approve_story
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from tests.integration.conftest import git


def make_project(tmp_path: Path, status: StoryStatus = StoryStatus.REFINED) -> tuple[Path, GitRepo]:
    root = tmp_path / "proj"
    (root / ".agent" / "domain").mkdir(parents=True)
    (root / ".agent" / ".gitignore").write_text("backlog/\nruns/\n")
    (root / ".agent" / "domain" / "glossary.md").write_text("# Glossary\n")
    (root / "README.md").write_text("hello\n")
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    story = Story(
        "S-0001",
        "t",
        status=status,
        acceptance_criteria=["c"],
        tasks=[Task("T1", "x")],
    )
    StoryStore(root).save(story)
    repo.commit_all("initial")
    return root, repo


def approve(root: Path, repo: GitRepo) -> Story:
    return approve_story(
        "S-0001", config=ProjectConfig(), project_root=root, repo=repo, store=StoryStore(root)
    )


def subject(root: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True
    ).stdout.strip()


def test_approve_commits_only_domain_docs(tmp_path: Path) -> None:
    root, repo = make_project(tmp_path)
    (root / ".agent" / "domain" / "glossary.md").write_text("# Glossary\nnew\n")
    (root / ".agent" / "domain" / "events.md").write_text("## S-0001: t\n")
    (root / "README.md").write_text("unrelated change\n")
    story = approve(root, repo)
    assert story.status == StoryStatus.APPROVED
    assert StoryStore(root).get("S-0001").status == StoryStatus.APPROVED
    assert subject(root) == "docs(S-0001): discovery and refinement"
    assert sorted(repo.changed_files(repo.head())) == [
        ".agent/domain/events.md",
        ".agent/domain/glossary.md",
    ]
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
    ).stdout
    assert status.strip() == "M README.md"


def test_approve_without_doc_changes_makes_no_commit(tmp_path: Path) -> None:
    root, repo = make_project(tmp_path)
    before = repo.head()
    assert approve(root, repo).status == StoryStatus.APPROVED
    assert repo.head() == before


def test_approve_requires_main_branch(tmp_path: Path) -> None:
    root, repo = make_project(tmp_path)
    repo.create_branch("story/S-0001", "main")
    with pytest.raises(RunError, match="main"):
        approve(root, repo)
    assert StoryStore(root).get("S-0001").status == StoryStatus.REFINED


def test_approve_requires_refined_story(tmp_path: Path) -> None:
    root, repo = make_project(tmp_path, StoryStatus.DISCOVERED)
    with pytest.raises(RunError):
        approve(root, repo)
    assert StoryStore(root).get("S-0001").status == StoryStatus.DISCOVERED
    with pytest.raises(RunError, match="not found"):
        approve_story(
            "S-9999",
            config=ProjectConfig(),
            project_root=root,
            repo=repo,
            store=StoryStore(root),
        )


def test_failed_doc_commit_leaves_story_refined(tmp_path: Path) -> None:
    root, repo = make_project(tmp_path)
    (root / ".agent" / "domain" / "glossary.md").write_text("# Glossary\nnew\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    with pytest.raises(RunError):
        approve(root, repo)
    assert StoryStore(root).get("S-0001").status == StoryStatus.REFINED
