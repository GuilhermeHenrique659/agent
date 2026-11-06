import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, ClassVar

import pytest
from claude_agent_sdk import ClaudeSDKError, ResultMessage
from pydantic import BaseModel

from autodev.adapters import claude_sdk_runtime
from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime, build_options, deny_git_hook
from autodev.config.models import AgentOverride, AgentsConfig, Effort
from autodev.domain.role import Role
from autodev.ports.agent_runtime import AgentError, AgentRequest


class Out(BaseModel):
    answer: int


def make_request(tmp_path: Path) -> AgentRequest[Out]:
    agents = AgentsConfig(
        roles={
            Role.IMPLEMENTER: AgentOverride(
                model="m",
                effort=Effort.LOW,
                max_turns=4,
                tools=["Read", "Bash"],
                max_budget_usd=1.5,
            )
        }
    )
    return AgentRequest(
        role=Role.IMPLEMENTER,
        profile=agents.profile_for(Role.IMPLEMENTER),
        instructions="be good",
        prompt="do it",
        workdir=tmp_path,
        output_model=Out,
    )


def result(
    structured: Any = None,
    *,
    is_error: bool = False,
    cost: float | None = 0.25,
    turns: int = 2,
    errors: list[str] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=turns,
        session_id="s",
        total_cost_usd=cost,
        structured_output=structured,
        errors=errors,
    )


def patch_query(
    monkeypatch: pytest.MonkeyPatch, results: list[ResultMessage | Exception]
) -> list[str]:
    prompts: list[str] = []
    queue = list(results)

    def fake_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        prompts.append(prompt)
        item = queue.pop(0)

        async def gen() -> AsyncIterator[Any]:
            if isinstance(item, Exception):
                raise item
            yield item

        return gen()

    monkeypatch.setattr(claude_sdk_runtime, "query", fake_query)
    return prompts


def test_build_options_maps_profile(tmp_path: Path) -> None:
    options = build_options(make_request(tmp_path))
    assert options.model == "m"
    assert options.effort == "low"
    assert options.max_turns == 4
    assert options.tools == ["Read", "Bash"]
    assert options.allowed_tools == ["Read", "Bash"]
    assert options.permission_mode == "dontAsk"
    assert options.cwd == tmp_path
    assert options.system_prompt == {
        "type": "preset",
        "preset": "claude_code",
        "append": "be good",
    }
    assert options.output_format == {"type": "json_schema", "schema": Out.model_json_schema()}
    assert options.max_budget_usd == 1.5
    assert options.setting_sources == ["project"]
    assert options.hooks is not None
    (matcher,) = options.hooks["PreToolUse"]
    assert matcher.matcher == "Bash"
    assert matcher.hooks == [deny_git_hook]


def test_build_options_passes_runtime_settings(tmp_path: Path) -> None:
    options = build_options(
        make_request(tmp_path),
        settings="/abs/auth.json",
        setting_sources=["user", "project"],
    )
    assert options.settings == "/abs/auth.json"
    assert options.setting_sources == ["user", "project"]
    assert build_options(make_request(tmp_path)).settings is None


def test_returns_validated_output_with_cost_and_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_query(monkeypatch, [result({"answer": 4})])
    outcome = asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))
    assert outcome.output == Out(answer=4)
    assert (outcome.cost_usd, outcome.turns) == (0.25, 2)


def test_retries_once_on_invalid_structured_output_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = patch_query(monkeypatch, [result({"answer": "x"}), result({"answer": 1}, turns=3)])
    outcome = asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))
    assert outcome.output == Out(answer=1)
    assert (outcome.cost_usd, outcome.turns) == (0.5, 5)
    assert prompts[0] == "do it"
    assert prompts[1].startswith("do it")
    assert "Your previous structured output was invalid" in prompts[1]


def test_raises_agent_error_after_second_invalid_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompts = patch_query(monkeypatch, [result(None), result({"nope": 1})])
    with pytest.raises(AgentError, match="invalid structured output"):
        asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))
    assert len(prompts) == 2


def test_raises_agent_error_when_result_is_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_query(monkeypatch, [result(is_error=True, errors=["boom"])])
    with pytest.raises(AgentError, match="boom"):
        asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))


def test_wraps_sdk_exceptions_and_missing_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_query(monkeypatch, [ClaudeSDKError("down")])
    with pytest.raises(AgentError, match="down"):
        asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))

    def empty_query(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            return
            yield

        return gen()

    monkeypatch.setattr(claude_sdk_runtime, "query", empty_query)
    with pytest.raises(AgentError, match="without a result"):
        asyncio.run(ClaudeSdkRuntime().run(make_request(tmp_path)))


def test_deny_git_hook_denies_forbidden_and_allows_other() -> None:
    hook: Callable[..., Any] = deny_git_hook
    denied = asyncio.run(
        hook({"tool_input": {"command": "git commit -m x"}}, "id", {"signal": None})
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denied["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    allowed = asyncio.run(hook({"tool_input": {"command": "pytest"}}, "id", {"signal": None}))
    assert allowed == {}


class FakeClient:
    instances: ClassVar[list["FakeClient"]] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        self.connected = False
        self.disconnects = 0
        self.sent: list[str] = []
        self.results: list[ResultMessage | Exception] = []
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str) -> None:
        self.sent.append(prompt)

    async def receive_response(self) -> AsyncIterator[Any]:
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        yield item

    async def disconnect(self) -> None:
        self.disconnects += 1


def patch_client(
    monkeypatch: pytest.MonkeyPatch, results: list[ResultMessage | Exception]
) -> FakeClient:
    FakeClient.instances = []
    original_init = FakeClient.__init__

    def init(self: FakeClient, options: Any) -> None:
        original_init(self, options)
        self.results = results

    monkeypatch.setattr(FakeClient, "__init__", init)
    monkeypatch.setattr(claude_sdk_runtime, "ClaudeSDKClient", FakeClient)
    return FakeClient  # type: ignore[return-value]


def test_conversation_sends_messages_and_validates_turns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_client(monkeypatch, [result({"answer": 1}), result({"answer": 2}, turns=3)])

    async def scenario() -> list[tuple[Out, float | None, int]]:
        conversation = await ClaudeSdkRuntime().start_conversation(make_request(tmp_path))
        client = FakeClient.instances[0]
        assert client.connected
        assert client.sent == []  # request.prompt is not sent automatically
        outcomes = [await conversation.send("hello"), await conversation.send("again")]
        assert client.sent == ["hello", "again"]
        return [(o.output, o.cost_usd, o.turns) for o in outcomes]

    assert asyncio.run(scenario()) == [(Out(answer=1), 0.25, 2), (Out(answer=2), 0.25, 3)]
    assert FakeClient.instances[0].options.model == "m"


def test_conversation_retries_invalid_output_once_in_same_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    patch_client(monkeypatch, [result({"answer": "x"}), result({"answer": 1}, turns=3)])

    async def scenario() -> tuple[int, float | None, int]:
        conversation = await ClaudeSdkRuntime().start_conversation(make_request(tmp_path))
        outcome = await conversation.send("hello")
        client = FakeClient.instances[0]
        assert client.sent[0] == "hello"
        assert client.sent[1].startswith("Your previous structured output was invalid:")
        assert client.sent[1].endswith("Return output matching the schema.")
        assert len(FakeClient.instances) == 1
        return outcome.output.answer, outcome.cost_usd, outcome.turns

    assert asyncio.run(scenario()) == (1, 0.5, 5)

    patch_client(monkeypatch, [result(None), result({"nope": 1})])

    async def failing() -> None:
        conversation = await ClaudeSdkRuntime().start_conversation(make_request(tmp_path))
        await conversation.send("hello")

    with pytest.raises(AgentError, match="invalid structured output"):
        asyncio.run(failing())

    patch_client(monkeypatch, [result(is_error=True, errors=["boom"])])
    with pytest.raises(AgentError, match="boom"):
        asyncio.run(failing())

    patch_client(monkeypatch, [ClaudeSDKError("down")])
    with pytest.raises(AgentError, match="down"):
        asyncio.run(failing())


def test_conversation_close_disconnects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    patch_client(monkeypatch, [])

    async def scenario() -> None:
        conversation = await ClaudeSdkRuntime().start_conversation(make_request(tmp_path))
        await conversation.close()
        await conversation.close()

    asyncio.run(scenario())
    assert FakeClient.instances[0].disconnects == 1
