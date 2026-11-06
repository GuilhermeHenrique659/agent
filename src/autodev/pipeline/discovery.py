from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.pipeline import prompts
from autodev.pipeline.outputs import DiscoveryResult, DiscoveryTurn
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

DONE_COMMAND = "/done"
_WRAP_UP = (
    "The user ended the discovery. Return done=true with the final result now, "
    "listing anything still unclear in open_questions."
)
_NO_RESULT = (
    "You marked the discovery as done but returned no result. Return done=true with the result."
)
_LOG_DETAIL_CHARS = 2000


async def run_discovery(
    story: Story,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    context: ContextSource,
    io: UserIO,
    log: RunLog,
) -> DiscoveryResult:
    role = Role.DISCOVERY
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    first = prompts.build_discovery_prompt(story.description, context.paths())
    request = AgentRequest(role, profile, instructions, first, project_root, DiscoveryTurn)
    max_turns = config.limits.max_discovery_turns
    try:
        conversation = await runtime.start_conversation(request)
    except AgentError as exc:
        log.append("discovery_finished", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    try:
        message = first
        turns = 0
        wrapping_up = False
        while True:
            outcome = await conversation.send(message)
            turn = outcome.output
            turns += 1
            log.append(
                "discovery_turn",
                role=role.value,
                ok=True,
                cost_usd=outcome.cost_usd,
                turns=outcome.turns,
                detail=turn.message[:_LOG_DETAIL_CHARS],
            )
            io.show(turn.message)
            if turn.done and turn.result is not None:
                log.append("discovery_finished", role=role.value, ok=True, detail=turn.result.title)
                return turn.result
            if wrapping_up:
                raise RunError("Discovery ended without a result after the turn limit")
            if turns >= max_turns:
                wrapping_up = True
                message = _WRAP_UP
            elif turn.done:
                message = _NO_RESULT
            else:
                answer = io.ask("> ")
                message = _WRAP_UP if answer.strip() == DONE_COMMAND else answer
    except AgentError as exc:
        log.append("discovery_finished", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    except RunError as exc:
        log.append("discovery_finished", role=role.value, ok=False, detail=str(exc))
        raise
    finally:
        await conversation.close()
