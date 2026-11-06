import re
from pathlib import Path

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.improve.metrics import compute_stats, load_records, render_stats
from autodev.improve.state import mark_improved
from autodev.pipeline import prompts
from autodev.pipeline.outputs import ProcessProposal, ProcessReview
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError, check_state_ignored
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_STANDARD_NAME = re.compile(r"^[\w-]+\.md$")
_COMMIT_PATHS = [".agent/process", ".agent/standards"]


class NoRunsYet(RunError):
    """There are no run logs to learn from yet."""


def _render_proposal(index: int, total: int, p: ProcessProposal) -> str:
    return (
        f"Proposal {index}/{total} [{p.kind}] {p.target}\n\n"
        f"Rationale: {p.rationale}\n\n{p.content.strip()}\n"
    )


def _ask_accept(io: UserIO) -> bool:
    while True:
        choice = io.ask("[a]ccept, [s]kip").strip().lower()
        if choice in ("a", "s"):
            return choice == "a"


def _apply(p: ProcessProposal, agent_dir: Path, io: UserIO) -> bool:
    """Write an accepted prompt/standard proposal; False (after a warning) if the target is bad."""
    if p.kind == "prompt":
        if p.target not in {r.value for r in Role}:
            io.show(f"Warning: unknown role {p.target!r}; proposal skipped.")
            return False
        path = agent_dir / "process" / f"{p.target}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(p.content)
        return True
    if not _STANDARD_NAME.match(p.target):
        io.show(f"Warning: invalid standards file name {p.target!r}; proposal skipped.")
        return False
    path = agent_dir / "standards" / p.target
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + "\n" + p.content.rstrip() + "\n")
    return True


async def run_process_retro(
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    repo: GitRepo,
    context: ContextSource,
    store: StoryStore,
    io: UserIO,
    log: RunLog,
) -> list[ProcessProposal]:
    main = config.git.main_branch
    if (branch := repo.current_branch()) != main:
        raise RunError(f"The retrospective must run on {main}, but the current branch is {branch}")
    if repo.has_changes():
        raise RunError("The working tree must be clean before the retrospective")
    check_state_ignored(repo)
    records = load_records(project_root)
    if not records:
        raise NoRunsYet("No runs yet; nothing to learn from")

    agent_dir = project_root / ".agent"
    effective = {
        role: prompts.load_instructions(role, config.agents.profile_for(role), agent_dir)
        for role in Role
    }
    role = Role.IMPROVER
    profile = config.agents.profile_for(role)
    prompt = prompts.build_process_retro_prompt(
        render_stats(compute_stats(records)), effective, context.paths()
    )
    request = AgentRequest(role, profile, effective[role], prompt, project_root, ProcessReview)
    try:
        outcome = await runtime.run(request)
    except AgentError as exc:
        log.append("improve_process", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    review = outcome.output
    log.append(
        "improve_process",
        role=role.value,
        ok=True,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        detail=review.summary,
    )
    io.show(review.summary)

    accepted: list[ProcessProposal] = []
    written = False
    total = len(review.proposals)
    for index, proposal in enumerate(review.proposals, start=1):
        io.show(_render_proposal(index, total, proposal))
        if not _ask_accept(io):
            log.append("proposal_skipped", detail=f"{proposal.kind}:{proposal.target}")
            continue
        if proposal.kind != "config":
            if not _apply(proposal, agent_dir, io):
                log.append("proposal_skipped", detail=f"{proposal.kind}:{proposal.target}")
                continue
            written = True
        accepted.append(proposal)
        log.append("proposal_accepted", detail=f"{proposal.kind}:{proposal.target}")

    if written:
        repo.commit_paths(_COMMIT_PATHS, "chore(process): apply retrospective proposals")
    config_changes = [p for p in accepted if p.kind == "config"]
    if config_changes:
        listed = "\n".join(f"- {p.target}: {p.content.strip()}" for p in config_changes)
        io.show(f"Recommended config changes (apply manually):\n{listed}")
    mark_improved(project_root, store)
    return accepted
