from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.pipeline import prompts
from autodev.pipeline.conversation import converse
from autodev.pipeline.outputs import DiscoveryResult, DiscoveryTurn
from autodev.pipeline.run_log import RunLog
from autodev.ports.agent_runtime import AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_WRAP_UP_NOTE = " List anything still unclear in open_questions."


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
    first = prompts.build_discovery_prompt(story.description, context.paths())
    return await converse(
        Role.DISCOVERY,
        first,
        DiscoveryTurn,
        config=config,
        project_root=project_root,
        runtime=runtime,
        io=io,
        log=log,
        event="discovery",
        wrap_up_note=_WRAP_UP_NOTE,
        summarize=lambda result: result.title,
    )
