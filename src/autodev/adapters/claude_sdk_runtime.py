from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
    query,
)
from pydantic import BaseModel, ValidationError

from autodev.adapters.git_guard import is_forbidden_git_command
from autodev.ports.agent_runtime import AgentError, AgentOutcome, AgentRequest

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


def build_options[T: BaseModel](request: AgentRequest[T]) -> ClaudeAgentOptions:
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
        setting_sources=["project"],
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[deny_git_hook])]},
    )


class ClaudeSdkRuntime:
    async def run[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]:
        options = build_options(request)
        cost: float | None = None
        turns = 0
        prompt = request.prompt
        for attempt in (1, 2):
            result = await self._query(prompt, options)
            turns += result.num_turns
            if result.total_cost_usd is not None:
                cost = (cost or 0.0) + result.total_cost_usd
            if result.is_error:
                detail = "; ".join(result.errors or []) or (result.result or "")
                raise AgentError(f"Agent run failed ({result.subtype}): {detail}")
            try:
                if result.structured_output is None:
                    raise ValueError("no structured output was returned")
                output = request.output_model.model_validate(result.structured_output)
            except (ValidationError, ValueError) as exc:
                if attempt == 2:
                    raise AgentError(f"Agent returned invalid structured output: {exc}") from exc
                prompt = f"{request.prompt}\n\n{_RETRY_NOTE.format(error=exc)}"
                continue
            return AgentOutcome(output=output, cost_usd=cost, turns=turns)
        raise AssertionError("unreachable")

    @staticmethod
    async def _query(prompt: str, options: ClaudeAgentOptions) -> ResultMessage:
        result: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        except ClaudeSDKError as exc:
            raise AgentError(f"Claude SDK error: {exc}") from exc
        if result is None:
            raise AgentError("Agent finished without a result message")
        return result
