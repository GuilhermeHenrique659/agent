from collections.abc import Sequence
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
    query,
)
from pydantic import BaseModel, ValidationError

from autodev.adapters.git_guard import is_forbidden_git_command
from autodev.config.models import SettingSource
from autodev.ports.agent_runtime import AgentError, AgentOutcome, AgentRequest, Conversation

_RETRY_NOTE = (
    "Your previous structured output was invalid: {error}. Return output matching the schema."
)


async def deny_git_hook(input_data: Any, tool_use_id: str | None, context: Any) -> Any:
    command = (input_data.get("tool_input") or {}).get("command", "")
    if isinstance(command, str) and is_forbidden_git_command(command):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "git commands that change the repository are not allowed; "
                    "the orchestrator owns git."
                ),
            }
        }
    return {}


def build_options[T: BaseModel](
    request: AgentRequest[T],
    *,
    settings: str | None = None,
    setting_sources: Sequence[SettingSource] = ("project",),
) -> ClaudeAgentOptions:
    profile = request.profile
    return ClaudeAgentOptions(
        model=profile.model,
        effort=profile.effort.value,
        max_turns=profile.max_turns,
        tools=list(profile.tools),
        allowed_tools=list(profile.tools),
        permission_mode="dontAsk",
        cwd=request.workdir,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": request.instructions,
        },
        output_format={
            "type": "json_schema",
            "schema": request.output_model.model_json_schema(),
        },
        max_budget_usd=profile.max_budget_usd,
        settings=settings,
        setting_sources=list(setting_sources),
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny_git_hook])]},
    )


class ClaudeSdkRuntime:
    def __init__(
        self,
        settings: str | None = None,
        setting_sources: Sequence[SettingSource] = ("project",),
    ) -> None:
        self.settings = settings
        self.setting_sources = list(setting_sources)

    async def run[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]:
        options = build_options(
            request, settings=self.settings, setting_sources=self.setting_sources
        )
        cost: float | None = None
        turns = 0
        prompt = request.prompt
        for attempt in (1, 2):
            result = await self._query(prompt, options)
            turns += result.num_turns
            cost = _add_cost(cost, result)
            try:
                output = _validated_output(result, request.output_model)
            except _InvalidOutput as exc:
                if attempt == 2:
                    raise AgentError(f"Agent returned invalid structured output: {exc}") from exc
                prompt = f"{request.prompt}\n\n{_RETRY_NOTE.format(error=exc)}"
                continue
            return AgentOutcome(output=output, cost_usd=cost, turns=turns)
        raise AssertionError("unreachable")

    async def start_conversation[T: BaseModel](self, request: AgentRequest[T]) -> Conversation[T]:
        options = build_options(
            request, settings=self.settings, setting_sources=self.setting_sources
        )
        client = ClaudeSDKClient(options)
        try:
            await client.connect()
        except ClaudeSDKError as exc:
            raise AgentError(f"Claude SDK error: {exc}") from exc
        return ClaudeSdkConversation(client, request.output_model)

    @staticmethod
    async def _query(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
        result: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as exc:
            raise AgentError(f"Claude SDK error: {exc}") from exc
        return _require_result(result)


class ClaudeSdkConversation[T: BaseModel]:
    def __init__(self, client: ClaudeSDKClient, output_model: type[T]) -> None:
        self._client = client
        self._output_model = output_model
        self._closed = False

    async def send(self, message: str) -> AgentOutcome[T]:
        cost: float | None = None
        turns = 0
        prompt = message
        for attempt in (1, 2):
            result = await self._exchange(prompt)
            turns += result.num_turns
            cost = _add_cost(cost, result)
            try:
                output = _validated_output(result, self._output_model)
            except _InvalidOutput as exc:
                if attempt == 2:
                    raise AgentError(f"Agent returned invalid structured output: {exc}") from exc
                prompt = _RETRY_NOTE.format(error=exc)
                continue
            return AgentOutcome(output=output, cost_usd=cost, turns=turns)
        raise AssertionError("unreachable")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.disconnect()

    async def _exchange(self, prompt: str) -> ResultMessage:
        result: ResultMessage | None = None
        try:
            await self._client.query(prompt)
            async for message in self._client.receive_response():
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as exc:
            raise AgentError(f"Claude SDK error: {exc}") from exc
        return _require_result(result)


class _InvalidOutput(Exception):
    pass


def _add_cost(cost: float | None, result: ResultMessage) -> float | None:
    if result.total_cost_usd is None:
        return cost
    return (cost or 0.0) + result.total_cost_usd


def _require_result(result: ResultMessage | None) -> ResultMessage:
    if result is None:
        raise AgentError("Agent finished without a result message")
    if result.is_error:
        detail = "; ".join(result.errors or []) or (result.result or "")
        raise AgentError(f"Agent run failed ({result.subtype}): {detail}")
    return result


def _validated_output[T: BaseModel](result: ResultMessage, model: type[T]) -> T:
    try:
        if result.structured_output is None:
            raise ValueError("no structured output was returned")
        return model.model_validate(result.structured_output)
    except (ValidationError, ValueError) as exc:
        raise _InvalidOutput(str(exc)) from exc
