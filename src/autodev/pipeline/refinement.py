import re
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

_TASK_NUMBER = re.compile(r"^\s*T\d+\s*[:.)\-\u2013]?\s*")


def _clean_title(title: str) -> str:
    cleaned = title
    while (stripped := _TASK_NUMBER.sub("", cleaned, count=1)) != cleaned:
        cleaned = stripped
    return cleaned.strip() or title


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
    max_tasks = config.limits.max_tasks_per_story
    attempts = config.limits.max_attempts
    current_feedback = feedback
    for attempt in range(1, attempts + 1):
        prompt = prompts.build_refiner_prompt(story, context.paths(), current_feedback, max_tasks)
        request = AgentRequest(
            Role.REFINER, profile, instructions, prompt, project_root, RefinementOutput
        )
        try:
            outcome = await runtime.run(request)
        except AgentError as exc:
            log.append("refinement", role=Role.REFINER.value, ok=False, detail=str(exc))
            raise RunError(str(exc)) from exc
        output = outcome.output
        count = len(output.tasks)
        if count <= max_tasks:
            break
        log.append(
            "refine_over_limit",
            role=Role.REFINER.value,
            ok=False,
            cost_usd=outcome.cost_usd,
            turns=outcome.turns,
            detail=f"attempt {attempt}: {count} tasks, limit {max_tasks}",
        )
        if attempt == attempts:
            raise RunError(
                f"The refiner produced {count} tasks after {attempts} attempts; "
                f"the limit is {max_tasks}"
            )
        consolidate = (
            f"You produced {count} tasks; the limit is {max_tasks}. Consolidate them into "
            f"at most {max_tasks} tasks, each still one red→green cycle."
        )
        current_feedback = f"{feedback}\n\n{consolidate}" if feedback else consolidate
    try:
        story.refine(
            output.title,
            render_refined(output.user_story, story.description),
            output.acceptance_criteria,
            [_clean_title(t.title) for t in output.tasks],
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
