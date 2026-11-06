from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.pipeline import prompts
from autodev.pipeline.adr import write_adr
from autodev.pipeline.conversation import ask_accept_or_feedback
from autodev.pipeline.outputs import ArchitectureReview, DiscoveryResult
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO


def render_architecture_review(review: ArchitectureReview) -> str:
    parts = [f"## Architecture proposal\n{review.summary}"]
    if review.adrs:
        parts.append(
            "### Decisions\n" + "\n".join(f"- {a.title}: {a.decision}" for a in review.adrs)
        )
    if review.components:
        parts.append("### Components\n" + "\n".join(f"- {c}" for c in review.components))
    if review.design_notes.strip():
        parts.append(f"### Design notes\n{review.design_notes.strip()}")
    return "\n\n".join(parts) + "\n"


async def review_architecture(
    story: Story,
    result: DiscoveryResult,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    context: ContextSource,
    io: UserIO,
    log: RunLog,
) -> str:
    """Architect proposes; the user accepts or sends feedback. Returns the section for the story."""
    role = Role.ARCHITECT
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    review: ArchitectureReview | None = None
    feedback: str | None = None
    while True:
        prompt = prompts.build_architecture_review_prompt(
            story, result, context.paths(), review, feedback
        )
        request = AgentRequest(
            role, profile, instructions, prompt, project_root, ArchitectureReview
        )
        try:
            outcome = await runtime.run(request)
        except AgentError as exc:
            log.append("architecture_review", role=role.value, ok=False, detail=str(exc))
            raise RunError(str(exc)) from exc
        review = outcome.output
        log.append(
            "architecture_review",
            role=role.value,
            ok=True,
            cost_usd=outcome.cost_usd,
            turns=outcome.turns,
            detail=review.summary,
        )
        io.show(render_architecture_review(review))
        feedback = ask_accept_or_feedback(io)
        if feedback is None:
            break
    lines = []
    for draft in review.adrs:
        path = write_adr(project_root, draft, "Accepted")
        relative = path.relative_to(project_root)
        log.append("adr_written", detail=str(relative))
        lines.append(f"- {relative}: {draft.title}")
    parts = ["## Architecture decisions" + "".join(f"\n{line}" for line in lines)]
    if review.components:
        parts.append("### Components\n" + "\n".join(f"- {c}" for c in review.components))
    if review.design_notes.strip():
        parts.append(f"### Design notes\n{review.design_notes.strip()}")
    return "\n\n".join(parts) + "\n"
