import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

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
