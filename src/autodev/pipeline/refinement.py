from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import InvalidTransition, Story
from autodev.pipeline import prompts
from autodev.pipeline.outputs import RefinementOutput
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_docs import render_refined
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource


async def refine_story(
    story: Story,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    context: ContextSource,
    log: RunLog,
    feedback: str | None = None,
) -> Story:
    profile = config.agents.profile_for(Role.REFINER)
    instructions = prompts.load_instructions(Role.REFINER, profile, project_root / ".agent")
    prompt = prompts.build_refiner_prompt(story, context.paths(), feedback)
    request = AgentRequest(
        Role.REFINER, profile, instructions, prompt, project_root, RefinementOutput
    )
    try:
        outcome = await runtime.run(request)
    except AgentError as exc:
        log.append("refinement", role=Role.REFINER.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    output = outcome.output
    try:
        story.refine(
            output.title,
            render_refined(output.user_story, story.description),
            output.acceptance_criteria,
            [t.title for t in output.tasks],
        )
    except InvalidTransition as exc:
        raise RunError(str(exc)) from exc
    log.append(
        "refinement",
        role=Role.REFINER.value,
        ok=True,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        detail=output.title,
    )
    return story
