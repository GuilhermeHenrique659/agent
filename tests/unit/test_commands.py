import asyncio
from pathlib import Path

import pytest

from autodev.app import commands
from autodev.app.commands import CommandError, RuntimeFactory
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.pipeline.state_store import StoryStore
from autodev.ports.agent_runtime import AgentRuntime
from tests.fakes import FakeAgentRuntime, FakeIO, Step
from tests.unit.test_cli import discovery_done, refined_output, setup_git_project


def factory(fake: FakeAgentRuntime) -> RuntimeFactory:
    def make(config: ProjectConfig, root: Path) -> AgentRuntime:
        return fake

    return make


def test_status_reports_through_io(tmp_path: Path) -> None:
    setup_git_project(tmp_path)
    StoryStore(tmp_path).save(Story("S-0001", "First story"))
    io = FakeIO()
    asyncio.run(commands.status(tmp_path, io))
    text = "\n".join(io.shown)
    assert "S-0001" in text
    assert "First story" in text
    assert "draft" in text


def test_request_flow_through_io_with_fake_runtime(tmp_path: Path) -> None:
    setup_git_project(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.DISCOVERY, discovery_done())
    fake.script(Role.REFINER, Step(refined_output()))
    io = FakeIO("a", "n")
    asyncio.run(commands.request(tmp_path, io, "Pay with card", make_runtime=factory(fake)))
    assert StoryStore(tmp_path).get("S-0001").status == StoryStatus.APPROVED
    assert io.prompts == ["[a]pprove, [f]eedback, [l]ater", "Implement now? [y/N]"]
    assert "S-0001: approved" in io.shown


def test_errors_become_command_error(tmp_path: Path) -> None:
    io = FakeIO()
    with pytest.raises(CommandError):
        asyncio.run(commands.status(tmp_path, io))
    setup_git_project(tmp_path)
    with pytest.raises(CommandError, match="S-9999 not found"):
        asyncio.run(commands.show(tmp_path, io, "S-9999"))
    with pytest.raises(CommandError, match="S-9999 not found"):
        asyncio.run(commands.resume(tmp_path, io, "S-9999"))
    with pytest.raises(CommandError, match="S-9999"):
        asyncio.run(commands.approve(tmp_path, io, "S-9999"))
