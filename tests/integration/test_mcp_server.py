import asyncio
from pathlib import Path
from typing import Any

from mcp.client.client import Client
from mcp.server import MCPServer

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.mcp.server import build_server
from autodev.mcp.sessions import SessionManager
from autodev.pipeline.state_store import StoryStore
from autodev.ports.agent_runtime import AgentRuntime
from tests.fakes import FakeAgentRuntime, Step
from tests.unit.test_cli import discovery_done, refined_output, setup_git_project

EXPECTED_TOOLS = {
    "autodev_onboard",
    "autodev_doctor",
    "autodev_status",
    "autodev_show",
    "autodev_check",
    "autodev_brief",
    "autodev_bootstrap",
    "autodev_request",
    "autodev_discover",
    "autodev_approve",
    "autodev_refine",
    "autodev_run",
    "autodev_resume",
    "autodev_improve",
    "autodev_reply",
    "autodev_poll",
    "autodev_cancel",
}


def make_server(fake: FakeAgentRuntime | None = None) -> MCPServer:
    runtime = fake or FakeAgentRuntime()

    def make_runtime(config: ProjectConfig, root: Path) -> AgentRuntime:
        return runtime

    return build_server(SessionManager(wait_seconds=10), make_runtime=make_runtime)


def test_lists_all_tools() -> None:
    async def scenario() -> set[str]:
        async with Client(make_server()) as client:
            return {tool.name for tool in (await client.list_tools()).tools}

    assert asyncio.run(scenario()) == EXPECTED_TOOLS


def test_status_tool_finishes_with_output(tmp_path: Path) -> None:
    setup_git_project(tmp_path)
    StoryStore(tmp_path).save(Story("S-0001", "First story"))

    async def scenario() -> Any:
        async with Client(make_server()) as client:
            return await client.call_tool("autodev_status", {"path": str(tmp_path)})

    result = asyncio.run(scenario())
    assert not result.is_error
    event = result.structured_content
    assert event["state"] == "finished"
    assert event["command"] == "status"
    assert "First story" in "\n".join(event["messages"])


def test_request_session_relays_question_and_reply(tmp_path: Path) -> None:
    setup_git_project(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.DISCOVERY, discovery_done())
    fake.script(Role.REFINER, Step(refined_output()))

    async def scenario() -> list[dict[str, Any]]:
        events = []
        async with Client(make_server(fake)) as client:
            args = {"path": str(tmp_path), "text": "Pay with card"}
            result = await client.call_tool("autodev_request", args)
            events.append(result.structured_content)
            for answer in ("a", "n"):
                result = await client.call_tool(
                    "autodev_reply", {"session_id": events[-1]["session_id"], "answer": answer}
                )
                events.append(result.structured_content)
        return events

    first, second, last = asyncio.run(scenario())
    assert first["state"] == "awaiting_reply"
    assert first["question"] == "[a]pprove, [f]eedback, [l]ater"
    assert second["state"] == "awaiting_reply"
    assert second["question"] == "Implement now? [y/N]"
    assert last["state"] == "finished"
    assert StoryStore(tmp_path).get("S-0001").status == StoryStatus.APPROVED


def test_unknown_session_is_a_tool_error() -> None:
    async def scenario() -> Any:
        async with Client(make_server()) as client:
            return await client.call_tool("autodev_poll", {"session_id": "nope"})

    result = asyncio.run(scenario())
    assert result.is_error
    assert "Unknown session: nope" in result.content[0].text
