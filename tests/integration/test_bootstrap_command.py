import asyncio
from pathlib import Path

import pytest

from autodev.app import commands
from autodev.app.commands import CommandError
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.pipeline.adr import write_adr
from autodev.pipeline.config_edit import set_commands
from autodev.pipeline.outputs import AdrDraft, BootstrapOutput, Commands
from autodev.ports.agent_runtime import AgentRuntime
from tests.fakes import FakeIO, Step
from tests.integration.test_briefing import (
    FLAG_CMD,
    TEST_CMD,
    Env,
    make_env,
    subjects,
    write_smoke,
)


def record_decisions(env: Env, test: str | None = TEST_CMD, adr: bool = True) -> None:
    """Simulate a finished briefing: ADR, stack.md and commands committed in a docs commit."""
    if adr:
        draft = AdrDraft(title="Use Python", context="c", decision="Python 3.12", consequences="q")
        write_adr(env.root, draft, "Accepted")
    (env.root / ".agent/standards").mkdir(parents=True, exist_ok=True)
    (env.root / ".agent/standards/stack.md").write_text("# Stack\n")
    set_commands(env.root, Commands(build=None, test=test, lint=None))
    env.repo.commit_all("docs: technical briefing")


def bootstrap(env: Env, io: FakeIO | None = None) -> FakeIO:
    io = io or FakeIO()

    def make_runtime(config: ProjectConfig, root: Path) -> AgentRuntime:
        return env.runtime

    asyncio.run(commands.bootstrap(env.root, io, make_runtime=make_runtime))
    return io


def test_bootstrap_command_creates_skeleton_from_recorded_decisions(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    record_decisions(env)
    env.runtime.script(
        Role.IMPLEMENTER, Step(BootstrapOutput(summary="skeleton"), write_smoke(True))
    )
    io = bootstrap(env)
    assert subjects(env.root)[:2] == [
        "chore(bootstrap): project skeleton",
        "docs: technical briefing",
    ]
    prompt = env.runtime.requests_for(Role.IMPLEMENTER)[0].prompt
    assert ".agent/architecture/adr/0001-use-python.md" in prompt
    assert ".agent/standards/stack.md" in prompt
    assert TEST_CMD in prompt
    assert (env.root / "smoke.py").is_file()
    assert any("Bootstrap" in text for text in io.shown)


def test_bootstrap_requires_decisions(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    with pytest.raises(CommandError, match="autodev brief"):
        bootstrap(env)


def test_bootstrap_refuses_repo_with_code(tmp_path: Path) -> None:
    env = make_env(tmp_path, existing_code=True)
    record_decisions(env)
    with pytest.raises(CommandError, match="already has code"):
        bootstrap(env)


def test_bootstrap_requires_test_command(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    record_decisions(env, test=None)
    with pytest.raises(CommandError, match="test command"):
        bootstrap(env)


def test_bootstrap_failure_resets_to_start_head(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    record_decisions(env, test=FLAG_CMD)
    start = env.repo.head()
    env.runtime.script(
        Role.IMPLEMENTER,
        Step(BootstrapOutput(summary="skeleton"), write_smoke(True)),
        Step(BootstrapOutput(summary="nothing")),
        Step(BootstrapOutput(summary="nothing")),
    )
    with pytest.raises(CommandError, match="flag missing"):
        bootstrap(env)
    assert env.repo.head() == start
    assert not (env.root / "smoke.py").exists()
