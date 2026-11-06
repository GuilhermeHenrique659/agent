import asyncio
from pathlib import Path

from autodev.adapters.git import GitRepo
from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config import load_config
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.improve.code_scan import run_code_scan
from autodev.pipeline.outputs import CodeReview, RefactoringProposal
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from tests.fakes import FakeIO, Step
from tests.integration.test_process_retro import Env, make_env


def proposals() -> list[RefactoringProposal]:
    return [
        RefactoringProposal(
            title=f"Refactor {n}",
            description=f"Do {n}",
            rationale=f"Because {n}",
            files=[f"src/{n}.py"],
        )
        for n in ("one", "two", "three")
    ]


def scan(env: Env, io: FakeIO) -> list[Story]:
    env.runtime.script(Role.IMPROVER, Step(CodeReview(summary="Scan", proposals=proposals())))
    return asyncio.run(
        run_code_scan(
            config=load_config(env.root),
            project_root=env.root,
            runtime=env.runtime,
            repo=GitRepo(env.root),
            context=KnowledgeFolder(env.root),
            store=StoryStore(env.root),
            io=io,
            log=RunLog(env.root, "improve"),
        )
    )


def test_selected_proposals_become_draft_stories(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    io = FakeIO("1, ,3")
    stories = scan(env, io)
    assert [s.title for s in stories] == ["Refactor one", "Refactor three"]
    assert all(s.status == StoryStatus.DRAFT for s in stories)
    saved = StoryStore(env.root).get(stories[0].id)
    assert saved.description == (
        "Do one\n\nRationale: Because one\n\nFiles: src/one.py\n\n"
        "Suggested by autodev improve --code."
    )
    assert {s.id for s in StoryStore(env.root).values()} == {s.id for s in stories}
    assert io.prompts[0] == "Create stories for which proposals? (e.g. 1,3 / all / none)"
    assert "1." in "\n".join(io.shown) and "Refactor two" in "\n".join(io.shown)
    assert (env.root / ".agent" / "runs" / "improve-state.json").is_file()
    prompt = env.runtime.requests_for(Role.IMPROVER)[0].prompt
    assert "README.md" in prompt  # churn is listed


def test_all_and_none_selection(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert len(scan(env, FakeIO("all"))) == 3
    assert scan(env, FakeIO("none")) == []
    assert scan(env, FakeIO("")) == []
    assert len(StoryStore(env.root).values()) == 3


def test_invalid_selection_reasks(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    io = FakeIO("abc", "0", "4", "1,9", "2")
    stories = scan(env, io)
    assert [s.title for s in stories] == ["Refactor two"]
    assert len(io.prompts) == 5


def test_long_titles_are_truncated(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(
        Role.IMPROVER,
        Step(
            CodeReview(
                summary="s",
                proposals=[RefactoringProposal(title="t" * 100, description="d", rationale="r")],
            )
        ),
    )
    stories = asyncio.run(
        run_code_scan(
            config=load_config(env.root),
            project_root=env.root,
            runtime=env.runtime,
            repo=GitRepo(env.root),
            context=KnowledgeFolder(env.root),
            store=StoryStore(env.root),
            io=FakeIO("1"),
            log=RunLog(env.root, "improve"),
        )
    )
    assert len(stories[0].title) == 60
