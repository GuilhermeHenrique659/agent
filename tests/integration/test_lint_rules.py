import asyncio
from pathlib import Path

import pytest

from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config import load_config
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.improve.lint_rules import run_lint_proposal
from autodev.pipeline.outputs import LintProposal, LintRule
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeIO, Step, agent_error
from tests.integration.test_process_retro import Env, make_env

PROMPT = "Create a story for which rules? (e.g. 1,3 / all / none)"


def rules() -> list[LintRule]:
    return [
        LintRule(tool="ruff", rule="T201", standard="logging.md", rationale="No print | use log"),
        LintRule(tool="mypy", rule="disallow_any_explicit", standard="typing.md", rationale="r2"),
        LintRule(tool="ruff", rule="B006", standard="style.md", rationale="r3"),
    ]


def propose(env: Env, io: FakeIO, step: Step | None = None) -> Story | None:
    env.runtime.script(
        Role.IMPROVER, step or Step(LintProposal(summary="Lint summary", rules=rules()))
    )
    return asyncio.run(
        run_lint_proposal(
            config=load_config(env.root),
            project_root=env.root,
            runtime=env.runtime,
            context=KnowledgeFolder(env.root),
            store=StoryStore(env.root),
            io=io,
            log=RunLog(env.root, "improve"),
        )
    )


def test_selected_rules_become_one_draft_story(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    io = FakeIO("1,3")
    story = propose(env, io)
    assert story is not None
    assert story.title == "Enforce standards with lint rules"
    assert story.status == StoryStatus.DRAFT
    saved = StoryStore(env.root).get(story.id)
    assert len(StoryStore(env.root).values()) == 1
    d = saved.description
    assert "## Rules" in d
    assert "| Tool | Rule | Enforces | Rationale |" in d
    assert "| ruff | T201 | logging.md | No print \\| use log |" in d
    assert "| ruff | B006 | style.md | r3 |" in d
    assert "disallow_any_explicit" not in d
    assert "## How to test" in d
    assert "fixture" in d and "red" in d and "green" in d
    # the violation fixture must not break the project's own lint gate
    assert "tests/" in d and "exclude" in d and "directly" in d
    assert "commands.lint" in d
    assert d.endswith("Suggested by autodev improve --lint.")
    assert io.prompts == [PROMPT]
    shown = "\n".join(io.shown)
    assert "Lint summary" in shown
    assert "1. [ruff] T201 — enforces: logging.md\n   No print | use log" in shown
    assert not (env.root / ".agent" / "runs" / "improve-state.json").exists()


def test_none_selected_creates_nothing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    assert propose(env, FakeIO("none")) is None
    assert StoryStore(env.root).values() == []


def test_no_rules_returns_none_without_asking(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    io = FakeIO()
    assert propose(env, io, Step(LintProposal(summary="Nothing to add"))) is None
    assert io.prompts == []
    assert "Nothing to add" in io.shown


def test_agent_error_becomes_run_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(RunError, match="boom"):
        propose(env, FakeIO(), Step(agent_error("boom")))


def test_prompt_includes_lint_command_and_context(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    config = env.root / ".agent" / "config.yaml"
    config.write_text(config.read_text().replace("lint: null", "lint: ruff check ."))
    propose(env, FakeIO("none"))
    prompt = env.runtime.requests_for(Role.IMPROVER)[0].prompt
    assert "ruff check ." in prompt
    assert "testing.md" in prompt


def test_prompt_says_none_configured(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    propose(env, FakeIO("none"))
    assert "none configured" in env.runtime.requests_for(Role.IMPROVER)[0].prompt
