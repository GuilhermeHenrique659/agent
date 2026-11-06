from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.pipeline import prompts
from autodev.pipeline.outputs import Turn
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.user_io import UserIO

DONE_COMMAND = "/done"
_LOG_DETAIL_CHARS = 2000


async def converse[R: BaseModel](
    role: Role,
    first_message: str,
    turn_model: type[Turn[R]],
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    io: UserIO,
    log: RunLog,
    event: str,
    wrap_up_note: str = "",
    summarize: Callable[[R], str] | None = None,
) -> R:
    """Chat with an agent until it returns done=true with a result (or the turn cap is hit)."""
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    request = AgentRequest(role, profile, instructions, first_message, project_root, turn_model)
    wrap_up = (
        f"The user ended the {event}. Return done=true with the final result now.{wrap_up_note}"
    )
    no_result = (
        f"You marked the {event} as done but returned no result. Return done=true with the result."
    )
    max_turns = config.limits.max_discovery_turns
    try:
        conversation = await runtime.start_conversation(request)
    except AgentError as exc:
        log.append(f"{event}_finished", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    try:
        message = first_message
        turns = 0
        wrapping_up = False
        while True:
            outcome = await conversation.send(message)
            turn = outcome.output
            turns += 1
            log.append(
                f"{event}_turn",
                role=role.value,
                ok=True,
                cost_usd=outcome.cost_usd,
                turns=outcome.turns,
                detail=turn.message[:_LOG_DETAIL_CHARS],
            )
            io.show(turn.message)
            if turn.done and turn.result is not None:
                detail = summarize(turn.result) if summarize else ""
                log.append(f"{event}_finished", role=role.value, ok=True, detail=detail)
                return turn.result
            if wrapping_up:
                raise RunError(f"The {event} ended without a result after the turn limit")
            if turns >= max_turns:
                wrapping_up = True
                message = wrap_up
            elif turn.done:
                message = no_result
            else:
                answer = io.ask("> ")
                message = wrap_up if answer.strip() == DONE_COMMAND else answer
    except AgentError as exc:
        log.append(f"{event}_finished", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    except RunError as exc:
        log.append(f"{event}_finished", role=role.value, ok=False, detail=str(exc))
        raise
    finally:
        await conversation.close()


def ask_accept_or_feedback(io: UserIO) -> str | None:
    """None when the user accepts, else their feedback; other answers are asked again."""
    while True:
        choice = io.ask("[a]ccept, [f]eedback").strip().lower()
        if choice == "a":
            return None
        if choice == "f":
            return io.ask("Feedback")
