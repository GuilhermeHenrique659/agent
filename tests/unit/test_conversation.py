import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from autodev.config.models import LimitsConfig, ProjectConfig
from autodev.domain.role import Role
from autodev.pipeline.conversation import converse
from autodev.pipeline.outputs import Turn
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error


class Payload(BaseModel):
    value: str


class PayloadTurn(Turn[Payload]):
    pass


RESULT = Payload(value="x")


def ask(text: str = "Q?") -> Step:
    return Step(PayloadTurn(message=text, done=False))


def finish(text: str = "Done") -> Step:
    return Step(PayloadTurn(message=text, done=True, result=RESULT))


def run(tmp_path: Path, runtime: FakeAgentRuntime, io: FakeIO, max_turns: int = 20) -> Payload:
    config = ProjectConfig(limits=LimitsConfig(max_discovery_turns=max_turns))
    return asyncio.run(
        converse(
            Role.ARCHITECT,
            "first message",
            PayloadTurn,
            config=config,
            project_root=tmp_path,
            runtime=runtime,
            io=io,
            log=RunLog(tmp_path, "chat"),
            event="chat",
        )
    )


def test_returns_result_when_agent_done(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("an answer")
    runtime.script(Role.ARCHITECT, ask("First?"), finish("All clear"))
    assert run(tmp_path, runtime, io) == RESULT
    assert io.shown == ["First?", "All clear"]
    assert runtime.messages_for(Role.ARCHITECT) == ["first message", "an answer"]
    assert runtime.conversations[0].closed
    assert runtime.requests[0].output_model is PayloadTurn
    log = next(iter((tmp_path / ".agent" / "runs" / "chat").glob("*.jsonl"))).read_text()
    assert "chat_turn" in log and "chat_finished" in log


def test_done_command_sends_wrap_up(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO(" /done ")
    runtime.script(Role.ARCHITECT, ask(), finish())
    assert run(tmp_path, runtime, io) == RESULT
    assert "The user ended the chat." in runtime.messages_for(Role.ARCHITECT)[1]


def test_turn_cap_then_error(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("a1", "a2")
    runtime.script(Role.ARCHITECT, ask(), ask(), ask())
    with pytest.raises(RunError):
        run(tmp_path, runtime, io, max_turns=2)
    messages = runtime.messages_for(Role.ARCHITECT)
    assert len(messages) == 3
    assert "The user ended the chat." in messages[2]
    assert runtime.conversations[0].closed


def test_done_without_result_asks_for_it(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO()
    runtime.script(Role.ARCHITECT, Step(PayloadTurn(message="ok", done=True)), finish())
    assert run(tmp_path, runtime, io) == RESULT
    assert "returned no result" in runtime.messages_for(Role.ARCHITECT)[1]
    assert io.prompts == []


def test_agent_error_closes_and_raises_run_error(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO()
    runtime.script(Role.ARCHITECT, Step(agent_error("boom")))
    with pytest.raises(RunError, match="boom"):
        run(tmp_path, runtime, io)
    assert runtime.conversations[0].closed
