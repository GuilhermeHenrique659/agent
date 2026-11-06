from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autodev.adapters.claude_settings import hooks_warning, project_hook_events
from autodev.adapters.git import GitError, GitRepo
from autodev.adapters.knowledge_folder import make_context
from autodev.config import load_config
from autodev.config.models import ProjectConfig
from autodev.onboarding.auth import minimal_auth_settings, write_auth_settings
from autodev.onboarding.doctor import DOCTOR_HINTS, check_connection, describe
from autodev.onboarding.scaffold import create_agent_dir, ensure_agent_gitignore
from autodev.pipeline.adr import has_decisions
from autodev.pipeline.briefing import is_empty_repo, run_brief
from autodev.pipeline.config_edit import set_value
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentRuntime
from autodev.ports.user_io import UserIO

_DOC_CANDIDATES = ("README.md", "docs/", "doc/", "documentation/")
_DEFAULT_BUDGET = "5"
_NO_CAP = ("none", "no", "-", "0")
_COMMIT_PATHS = [".agent", "CLAUDE.md"]
_IMPORT_LINE = "@AGENTS.md"
_REQUEST_STEP = 'autodev request "<what you want>"'


class OnboardError(Exception):
    pass


@dataclass(frozen=True)
class OnboardReport:
    actions: list[str]
    next_steps: list[str]


def confirm(io: UserIO, question: str, default: bool, yes: bool) -> bool:
    if yes:
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = io.ask(f"{question} {suffix}").strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def ask_value(io: UserIO, question: str, default: str, yes: bool) -> str:
    if yes:
        return default
    return io.ask(f"{question} [{default}]").strip() or default


async def onboard(
    path: Path,
    *,
    io: UserIO,
    make_runtime: Callable[[ProjectConfig, Path], AgentRuntime],
    yes: bool,
    run_brief_step: bool,
    home: Path,
) -> OnboardReport:
    """Integrate autodev into a new or existing project; safe to run again."""
    actions: list[str] = []
    next_steps: list[str] = []
    repo = GitRepo(path)
    _ensure_directory(path, io, yes, actions)
    _ensure_git(path, repo, io, yes, actions)
    _ensure_agent_dir(path, actions)
    _configure(path, repo, io, yes, home, actions, next_steps)
    _check_conventions(path, io, yes, actions)
    config = load_config(path)
    runtime = make_runtime(config, path)
    result = await check_connection(config, path, runtime)
    if not result.ok:
        raise OnboardError(f"{result.detail}\n{DOCTOR_HINTS}")
    actions.append(describe(result))
    _commit(repo, actions)
    if run_brief_step:
        await _brief(path, repo, config, runtime, io, yes, actions, next_steps)
    else:
        next_steps.append("autodev brief")
    next_steps.append(_REQUEST_STEP)
    return OnboardReport(actions, next_steps)


def _ensure_directory(path: Path, io: UserIO, yes: bool, actions: list[str]) -> None:
    if path.is_dir():
        return
    if not confirm(io, f"Create {path}?", True, yes):
        raise OnboardError("Nothing to do")
    path.mkdir(parents=True)
    actions.append(f"Created {path}")


def _ensure_git(path: Path, repo: GitRepo, io: UserIO, yes: bool, actions: list[str]) -> None:
    if repo.is_repo():
        return
    if any(path.iterdir()) and not confirm(io, "Initialize a git repository here?", True, yes):
        raise OnboardError("Nothing to do: autodev needs a git repository")
    repo.init("main")
    actions.append("Initialized a git repository (branch main)")


def _ensure_agent_dir(path: Path, actions: list[str]) -> None:
    if (path / ".agent").exists():
        actions.append("already onboarded; checking configuration")
    else:
        create_agent_dir(path)
        actions.append("Created .agent/")
    if ensure_agent_gitignore(path):
        actions.append("Updated .agent/.gitignore")


def _configure(
    path: Path,
    repo: GitRepo,
    io: UserIO,
    yes: bool,
    home: Path,
    actions: list[str],
    next_steps: list[str],
) -> None:
    config = load_config(path)
    branch = repo.current_branch()
    if branch != config.git.main_branch:
        answer = ask_value(io, "Main branch", branch, yes)
        set_value(path, "git.main_branch", answer)
        actions.append(f"Set git.main_branch to {answer}")
    remotes = repo.remotes() if config.git.remote is None else []
    if remotes and confirm(io, f"Allow autodev to pull/push to {remotes[0]}?", False, yes):
        set_value(path, "git.remote", remotes[0])
        actions.append(f"Set git.remote to {remotes[0]}")
    _configure_auth(path, config, io, yes, home, actions, next_steps)
    if config.agents.defaults.max_budget_usd is None:
        budget = _ask_budget(io, yes)
        if budget is not None:
            set_value(path, "agents.defaults.max_budget_usd", budget)
            actions.append(f"Set agents.defaults.max_budget_usd to {budget}")
    _configure_docs(path, config, io, yes, actions)


def _configure_auth(
    path: Path,
    config: ProjectConfig,
    io: UserIO,
    yes: bool,
    home: Path,
    actions: list[str],
    next_steps: list[str],
) -> None:
    if config.runtime.settings is not None or "user" in config.runtime.setting_sources:
        return
    settings = minimal_auth_settings(home)
    question = "Use your Claude Code authentication (minimal settings in .agent/auth.json)?"
    if settings is not None and confirm(io, question, True, yes):
        write_auth_settings(path, settings)
        set_value(path, "runtime.settings", "auth.json")
        actions.append("Wrote .agent/auth.json and set runtime.settings")
    else:
        next_steps.append("Configure authentication (runtime.settings or ANTHROPIC_API_KEY)")


def _ask_budget(io: UserIO, yes: bool) -> float | None:
    question = "Max cost per agent run in USD (none = no cap)"
    while True:
        answer = ask_value(io, question, _DEFAULT_BUDGET, yes).lower()
        if answer in _NO_CAP:
            return None
        try:
            value = float(answer)
        except ValueError:
            continue
        if value > 0:
            return value


def _configure_docs(
    path: Path, config: ProjectConfig, io: UserIO, yes: bool, actions: list[str]
) -> None:
    current = config.context.paths
    known = {p.rstrip("/") for p in current}
    found = [c for c in _DOC_CANDIDATES if (path / c).exists() and c.rstrip("/") not in known]
    if found and confirm(io, f"Use these as agent context: {', '.join(found)}?", True, yes):
        set_value(path, "context.paths", [*current, *found])
        actions.append(f"Added to context.paths: {', '.join(found)}")


def _check_conventions(path: Path, io: UserIO, yes: bool, actions: list[str]) -> None:
    found = [f for f in ("CLAUDE.md", "AGENTS.md") if (path / f).is_file()]
    if any((path / ".claude" / "rules").rglob("*.md")):
        found.append(".claude/rules/")
    skills = sorted(p.parent.name for p in (path / ".claude" / "skills").glob("*/SKILL.md"))
    if skills:
        found.append(f"skills ({', '.join(skills)})")
    actions.append(f"Conventions found: {', '.join(found)}" if found else "No conventions found")
    _offer_agents_import(path, io, yes, actions)
    if events := project_hook_events(path):
        io.show(hooks_warning(events))


def _offer_agents_import(path: Path, io: UserIO, yes: bool, actions: list[str]) -> None:
    if not (path / "AGENTS.md").is_file():
        return
    claude = path / "CLAUDE.md"
    text = claude.read_text() if claude.is_file() else None
    if text is not None and _IMPORT_LINE in (line.strip() for line in text.splitlines()):
        return
    if not confirm(io, "Add @AGENTS.md to CLAUDE.md so agents load it?", True, yes):
        return
    if text is None:
        claude.write_text(f"{_IMPORT_LINE}\n")
    else:
        claude.write_text(f"{text}\n{_IMPORT_LINE}\n")
    actions.append("Added @AGENTS.md to CLAUDE.md")


def _commit(repo: GitRepo, actions: list[str]) -> None:
    try:
        sha = repo.commit_paths(_COMMIT_PATHS, "chore: onboard autodev")
    except GitError as exc:
        raise OnboardError(str(exc)) from exc
    actions.append(f"Committed: chore: onboard autodev ({sha[:7]})" if sha else "Nothing to commit")


async def _brief(
    path: Path,
    repo: GitRepo,
    config: ProjectConfig,
    runtime: AgentRuntime,
    io: UserIO,
    yes: bool,
    actions: list[str],
    next_steps: list[str],
) -> None:
    config = load_config(path)
    if has_decisions(path):
        actions.append("Technical briefing already done")
        return
    if not confirm(io, "Run the technical briefing now?", True, yes):
        next_steps.append("autodev brief")
        return
    if repo.has_changes():
        next_steps.append("Commit or stash your changes, then run `autodev brief`")
        return
    was_empty = is_empty_repo(repo)
    try:
        await run_brief(
            config=config,
            project_root=path,
            runtime=runtime,
            repo=repo,
            context=make_context(config, path),
            io=io,
            log=RunLog(path, "brief"),
        )
    except RunError as exc:
        actions.append(f"Technical briefing failed: {exc}")
        next_steps.append("autodev brief")
        return
    actions.append("Technical briefing done" + (" (project skeleton created)" if was_empty else ""))
