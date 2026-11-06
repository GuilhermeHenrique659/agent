from pathlib import Path

from autodev.adapters.git import GitError, GitRepo
from autodev.config import load_config
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.gates.runner import run_gates
from autodev.pipeline import prompts
from autodev.pipeline.adr import write_adr
from autodev.pipeline.config_edit import set_commands
from autodev.pipeline.conversation import ask_accept_or_feedback, converse
from autodev.pipeline.outputs import BootstrapOutput, TechBrief, TechBriefTurn
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError, check_state_ignored
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_DOC_PATHS = [".agent/architecture", ".agent/standards", ".agent/config.yaml"]
_LOG_DETAIL_CHARS = 2000


def is_empty_repo(repo: GitRepo) -> bool:
    """Empty for briefing purposes: no tracked file outside .agent/."""
    return not any(not f.startswith(".agent/") for f in repo.tracked_files())


def render_tech_brief(brief: TechBrief) -> str:
    adrs = "\n".join(f"- {a.title}: {a.decision}" for a in brief.adrs)
    c = brief.commands
    commands = "\n".join(
        f"- {name}: {value}"
        for name, value in (("build", c.build), ("test", c.test), ("lint", c.lint))
    )
    parts = [f"## Summary\n{brief.summary}", f"## Decisions\n{adrs}", f"## Commands\n{commands}"]
    if brief.standards:
        parts.append("## Standards\n" + "\n".join(f"- {s}" for s in brief.standards))
    return "\n\n".join(parts) + "\n"


async def run_brief(
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    repo: GitRepo,
    context: ContextSource,
    io: UserIO,
    log: RunLog,
) -> TechBrief:
    """Technical briefing chat -> ADRs, standards and commands (by code) -> bootstrap if empty."""
    main = config.git.main_branch
    if (branch := repo.current_branch()) != main:
        raise RunError(f"The briefing must happen on {main}, but the current branch is {branch}")
    if repo.has_changes():
        raise RunError("Working tree has uncommitted changes; commit or discard them first")
    check_state_ignored(repo)
    empty = is_empty_repo(repo)
    brief = await _chat(
        prompts.build_brief_prompt(context.paths(), empty),
        config=config,
        project_root=project_root,
        runtime=runtime,
        io=io,
        log=log,
    )
    while True:
        io.show(render_tech_brief(brief))
        feedback = ask_accept_or_feedback(io)
        if feedback is None:
            break
        brief = await _chat(
            prompts.build_brief_feedback_prompt(brief, feedback),
            config=config,
            project_root=project_root,
            runtime=runtime,
            io=io,
            log=log,
        )
    docs_sha = _write_docs(brief, project_root, repo, log)
    config = load_config(project_root)
    if empty:
        if config.commands.test is None:
            raise RunError("The technical brief defined no test command")
        await _bootstrap(
            brief,
            docs_sha,
            config=config,
            project_root=project_root,
            runtime=runtime,
            repo=repo,
            context=context,
            log=log,
        )
    return brief


async def _chat(
    first_message: str,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    io: UserIO,
    log: RunLog,
) -> TechBrief:
    return await converse(
        Role.ARCHITECT,
        first_message,
        TechBriefTurn,
        config=config,
        project_root=project_root,
        runtime=runtime,
        io=io,
        log=log,
        event="brief",
        summarize=lambda brief: brief.summary[:_LOG_DETAIL_CHARS],
    )


def _convention_files(project_root: Path) -> list[str]:
    found = [Path(f) for f in ("CLAUDE.md", "AGENTS.md") if (project_root / f).is_file()]
    found += (p.relative_to(project_root) for p in (project_root / ".claude/rules").rglob("*.md"))
    return sorted({p.as_posix() for p in found})


def _write_docs(brief: TechBrief, project_root: Path, repo: GitRepo, log: RunLog) -> str:
    for draft in brief.adrs:
        path = write_adr(project_root, draft, "Accepted")
        log.append("adr_written", detail=str(path.relative_to(project_root)))
    standards = project_root / ".agent" / "standards"
    standards.mkdir(parents=True, exist_ok=True)
    bullets = "".join(f"- {s}\n" for s in brief.standards)
    stack = f"# Stack and standards\n\n{brief.summary}\n\n{bullets}"
    if conventions := _convention_files(project_root):
        stack += f"\nProject conventions also live in: {', '.join(conventions)}.\n"
    (standards / "stack.md").write_text(stack)
    set_commands(project_root, brief.commands)
    try:
        repo.commit_paths(_DOC_PATHS, "docs: technical briefing")
    except GitError as exc:
        raise RunError(str(exc)) from exc
    return repo.head()


async def _bootstrap(
    brief: TechBrief,
    docs_sha: str,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    repo: GitRepo,
    context: ContextSource,
    log: RunLog,
) -> None:
    role = Role.IMPLEMENTER
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    feedback: str | None = None
    committed = False
    for _ in range(config.limits.max_attempts):
        before = repo.head()
        prompt = prompts.build_bootstrap_prompt(brief, context.paths(), feedback)
        request = AgentRequest(role, profile, instructions, prompt, project_root, BootstrapOutput)
        try:
            outcome = await runtime.run(request)
        except AgentError as exc:
            log.append("bootstrap", role=role.value, ok=False, detail=str(exc))
            feedback = str(exc)
            repo.reset_hard(before)
            continue
        log.append(
            "bootstrap",
            role=role.value,
            ok=True,
            cost_usd=outcome.cost_usd,
            turns=outcome.turns,
            detail=outcome.output.summary[:_LOG_DETAIL_CHARS],
        )
        if not repo.has_changes():
            feedback = "The implementer made no changes."
            continue
        prefix = "fix" if committed else "chore"
        repo.commit_all(f"{prefix}(bootstrap): project skeleton")
        committed = True
        report = run_gates(config, project_root)
        detail = "\n".join(
            f"{r.name}: {'ok' if r.ok else 'failed'}" + ("" if r.ok else f"\n{r.output}")
            for r in report.results
        )
        log.append("gate", ok=report.ok, detail=detail[-_LOG_DETAIL_CHARS:])
        if report.ok:
            return
        feedback = prompts.gate_feedback(report)
    repo.reset_hard(docs_sha)
    raise RunError(f"Bootstrap failed: {feedback}")
