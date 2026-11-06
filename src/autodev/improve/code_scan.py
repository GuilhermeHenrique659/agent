from pathlib import Path

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.improve.selection import ask_selection
from autodev.improve.state import mark_improved
from autodev.pipeline import prompts
from autodev.pipeline.outputs import CodeReview
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_TITLE_CHARS = 60
_PROMPT = "Create stories for which proposals? (e.g. 1,3 / all / none)"


async def run_code_scan(
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    repo: GitRepo,
    context: ContextSource,
    store: StoryStore,
    io: UserIO,
    log: RunLog,
) -> list[Story]:
    role = Role.IMPROVER
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    prompt = prompts.build_code_scan_prompt(repo.churn(), context.paths())
    request = AgentRequest(role, profile, instructions, prompt, project_root, CodeReview)
    try:
        outcome = await runtime.run(request)
    except AgentError as exc:
        log.append("improve_code", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    review = outcome.output
    log.append(
        "improve_code",
        role=role.value,
        ok=True,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        detail=review.summary,
    )
    io.show(review.summary)
    created: list[Story] = []
    if review.proposals:
        for n, p in enumerate(review.proposals, start=1):
            files = ", ".join(p.files)
            io.show(f"{n}. {p.title}\n{p.description}\nRationale: {p.rationale}\nFiles: {files}\n")
        for index in ask_selection(io, _PROMPT, len(review.proposals)):
            p = review.proposals[index]
            description = (
                f"{p.description}\n\nRationale: {p.rationale}\n\n"
                f"Files: {', '.join(p.files)}\n\nSuggested by autodev improve --code."
            )
            story = Story(store.next_id(), p.title[:_TITLE_CHARS], description=description)
            store.save(story)
            log.append("improvement_created", detail=story.id)
            created.append(story)
    mark_improved(project_root, store)
    return created
