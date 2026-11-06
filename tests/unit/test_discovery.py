import asyncio
from pathlib import Path

import pytest

from autodev.config.models import LimitsConfig, ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.pipeline.discovery import run_discovery
from autodev.pipeline.outputs import DiscoveryResult, DiscoveryTurn
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error

RESULT = DiscoveryResult(title="Checkout", problem="Pay")
WRAP_UP = "The user ended the discovery."


class Context:
    def paths(self) -> list[Path]:
        return [Path(".agent/domain/glossary.md")]


def ask(text: str = "Q?") -> Step:
    return Step(DiscoveryTurn(message=text, done=False))


def finish(text: str = "Done") -> Step:
    return Step(DiscoveryTurn(message=text, done=True, result=RESULT))


def run(
    tmp_path: Path, runtime: FakeAgentRuntime, io: FakeIO, max_turns: int = 20
) -> DiscoveryResult:
    config = ProjectConfig(limits=LimitsConfig(max_discovery_turns=max_turns))
    story = Story("S-0001", "Pay", description="I want to pay")
    return asyncio.run(
        run_discovery(
            story,
            config=config,
            project_root=tmp_path,
            runtime=runtime,
            context=Context(),
            io=io,
            log=RunLog(tmp_path, "S-0001"),
        )
    )


def test_agent_finishes_with_result(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("an answer")
    runtime.script(Role.DISCOVERY, ask("First?"), finish("All clear"))
    assert run(tmp_path, runtime, io) == RESULT
    assert io.shown == ["First?", "All clear"]
    assert io.prompts == ["> "]
    assert runtime.messages_for(Role.DISCOVERY)[1:] == ["an answer"]
    assert runtime.conversations[0].closed
    assert runtime.requests[0].output_model is DiscoveryTurn
    log = (tmp_path / ".agent" / "runs" / "S-0001").glob("*.jsonl")
    assert "discovery_turn" in next(iter(log)).read_text()


def test_user_done_command_requests_wrap_up(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("  /done ")
    runtime.script(Role.DISCOVERY, ask(), finish())
    assert run(tmp_path, runtime, io) == RESULT
    assert WRAP_UP in runtime.messages_for(Role.DISCOVERY)[1]
    assert "open_questions" in runtime.messages_for(Role.DISCOVERY)[1]


def test_done_without_result_asks_for_result(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO()
    runtime.script(Role.DISCOVERY, Step(DiscoveryTurn(message="ok", done=True)), finish())
    assert run(tmp_path, runtime, io) == RESULT
    assert runtime.messages_for(Role.DISCOVERY)[1] == (
        "You marked the discovery as done but returned no result. Return done=true with the result."
    )
    assert io.prompts == []


def test_turn_limit_forces_wrap_up_then_errors(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("a1", "a2")
    runtime.script(Role.DISCOVERY, ask(), ask(), ask())
    with pytest.raises(RunError):
        run(tmp_path, runtime, io, max_turns=2)
    messages = runtime.messages_for(Role.DISCOVERY)
    assert len(messages) == 3
    assert messages[1] == "a1"
    assert WRAP_UP in messages[2]
    assert io.prompts == ["> "]
    assert runtime.conversations[0].closed

    runtime2, io2 = FakeAgentRuntime(), FakeIO("a1")
    runtime2.script(Role.DISCOVERY, ask(), ask(), finish())
    assert run(tmp_path, runtime2, io2, max_turns=2) == RESULT


def test_agent_error_becomes_run_error_and_conversation_closed(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO()
    runtime.script(Role.DISCOVERY, Step(agent_error("boom")))
    with pytest.raises(RunError, match="boom"):
        run(tmp_path, runtime, io)
    assert runtime.conversations[0].closed


def test_first_message_contains_request_and_context(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO()
    runtime.script(Role.DISCOVERY, finish())
    run(tmp_path, runtime, io)
    first = runtime.messages_for(Role.DISCOVERY)[0]
    assert "I want to pay" in first
    assert ".agent/domain/glossary.md" in first
    assert "language of the request" in first
