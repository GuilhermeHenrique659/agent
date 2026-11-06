import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime
from autodev.adapters.claude_settings import hooks_warning, project_hook_events
from autodev.adapters.console_io import ConsoleIO
from autodev.adapters.git import GitError, GitRepo
from autodev.adapters.knowledge_folder import make_context
from autodev.config import ConfigError, load_config
from autodev.config.models import ProjectConfig
from autodev.domain.story import InvalidTransition, Story, StoryStatus
from autodev.gates.runner import run_gates
from autodev.improve.code_scan import run_code_scan
from autodev.improve.lint_rules import run_lint_proposal
from autodev.improve.process_retro import NoRunsYet, run_process_retro
from autodev.improve.state import new_done_count, should_suggest
from autodev.onboarding.doctor import DOCTOR_HINTS, check_connection, describe
from autodev.onboarding.onboard import OnboardError
from autodev.onboarding.onboard import onboard as run_onboard
from autodev.onboarding.scaffold import create_agent_dir
from autodev.pipeline.adr import adr_dir, has_decisions
from autodev.pipeline.approval import approve_story
from autodev.pipeline.briefing import is_empty_repo, run_brief
from autodev.pipeline.refinement import refine_story
from autodev.pipeline.request_flow import discover_and_refine, handle_request
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_docs import render_story_summary
from autodev.pipeline.story_runner import RunError, StoryRunner
from autodev.ports.agent_runtime import AgentRuntime
from autodev.ports.user_io import UserIO

app = typer.Typer(help="Orchestrate AI agents to build software.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

PathArg = Annotated[Path, typer.Argument(help="Project root (default: current directory).")]
PathOpt = Annotated[Path, typer.Option(help="Project root.")]


@app.command()
def init(path: PathArg = Path()) -> None:
    """Create the .agent/ folder from the template."""
    target = path / ".agent"
    if target.exists():
        err_console.print(f"[red]Error:[/red] {target} already exists.")
        raise typer.Exit(1)
    create_agent_dir(path)
    console.print(f"Created {target}")
    _warn_about_hooks(path)


def _warn_about_hooks(path: Path) -> None:
    if events := project_hook_events(path):
        console.print(hooks_warning(events), markup=False)


@app.command()
def onboard(
    path: PathArg = Path(),
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept every default without asking.")
    ] = False,
    no_brief: Annotated[
        bool, typer.Option("--no-brief", help="Skip the technical briefing step.")
    ] = False,
) -> None:
    """Integrate autodev into a new or existing project (idempotent, interactive)."""
    try:
        report = asyncio.run(
            run_onboard(
                path,
                io=make_io(),
                make_runtime=make_runtime,
                yes=yes,
                run_brief_step=not no_brief,
                home=Path.home(),
            )
        )
    except (OnboardError, ConfigError, GitError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}", markup=True, highlight=False)
        raise typer.Exit(1) from exc
    for action in report.actions:
        console.print(f"- {action}", markup=False, soft_wrap=True)
    console.print("Next steps:")
    for step in report.next_steps:
        console.print(f"- {step}", markup=False, soft_wrap=True)


@app.command()
def doctor(path: PathOpt = Path()) -> None:
    """Check that the agent runtime can reach the model (one cheap call)."""
    try:
        config = load_config(path)
        result = asyncio.run(check_connection(config, path, make_runtime(config, path)))
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    if not result.ok:
        err_console.print(result.detail, markup=False, highlight=False)
        err_console.print(DOCTOR_HINTS, markup=False, highlight=False)
        raise typer.Exit(1)
    console.print(describe(result), markup=False)


@app.command()
def status(path: PathArg = Path()) -> None:
    """List the stories in the backlog."""
    try:
        load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    stories = StoryStore(path).values()
    if not stories:
        console.print("No stories yet.")
        return
    table = Table()
    for column in ("id", "title", "status", "attempts"):
        table.add_column(column)
    for s in stories:
        table.add_row(s.id, s.title, s.status.value, str(s.attempts))
    console.print(table)


@app.command()
def check(path: PathArg = Path()) -> None:
    """Run the quality gates (build, test, lint)."""
    try:
        config = load_config(path)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    report = run_gates(config, path)
    table = Table()
    table.add_column("gate")
    table.add_column("result")
    for r in report.results:
        table.add_row(r.name, "skipped" if r.skipped else "passed" if r.ok else "failed")
    console.print(table)
    if not report.ok:
        for r in report.failures:
            err_console.print(f"[red]{r.name} failed:[/red]")
            err_console.print(r.output, markup=False, highlight=False)
        raise typer.Exit(1)


def make_runtime(config: ProjectConfig, root: Path) -> AgentRuntime:
    """Factory for the agent runtime; tests monkeypatch it."""
    settings = config.runtime.settings
    if settings is not None:
        settings_path = Path(settings).expanduser()
        if not settings_path.is_absolute():
            settings_path = root / ".agent" / settings_path
        if not settings_path.is_file():
            raise ConfigError(f"runtime.settings file not found: {settings_path}")
        settings = str(settings_path)
    return ClaudeSdkRuntime(settings=settings, setting_sources=config.runtime.setting_sources)


def make_io() -> UserIO:
    """Factory for the user IO; tests monkeypatch it."""
    return ConsoleIO()


def _run_brief(
    config: ProjectConfig, path: Path, runtime: AgentRuntime, io: UserIO
) -> ProjectConfig:
    """Run the technical briefing, print what it wrote and return the reloaded config."""
    repo = GitRepo(path)
    was_empty = is_empty_repo(repo)
    before = {p.name for p in adr_dir(path).glob("*.md")}
    brief = asyncio.run(
        run_brief(
            config=config,
            project_root=path,
            runtime=runtime,
            repo=repo,
            context=make_context(config, path),
            io=io,
            log=RunLog(path, "brief"),
        )
    )
    for adr in sorted(p for p in adr_dir(path).glob("*.md") if p.name not in before):
        console.print(f"ADR: {adr.relative_to(path)}", markup=False)
    for name in ("build", "test", "lint"):
        console.print(f"{name}: {getattr(brief.commands, name)}", markup=False)
    console.print(f"Bootstrap: {'ran' if was_empty else 'skipped'}")
    return load_config(path)


def _offer_brief(
    config: ProjectConfig, path: Path, runtime: AgentRuntime, io: UserIO
) -> ProjectConfig:
    if has_decisions(path):
        return config
    answer = io.ask("No technical decisions recorded yet. Run the technical briefing first? [Y/n]")
    if answer.strip().lower() in ("", "y", "yes"):
        return _run_brief(config, path, runtime, io)
    return config


def _run_story(config: ProjectConfig, path: Path, runtime: AgentRuntime, story_id: str) -> Story:
    runner = StoryRunner(
        config,
        path,
        runtime,
        GitRepo(path),
        StoryStore(path),
        make_context(config, path),
        lambda sid: RunLog(path, sid),
    )
    return asyncio.run(runner.run(story_id))


def _print_final_status(story: Story, config: ProjectConfig, path: Path) -> None:
    console.print(f"{story.id}: {story.status.value}")
    if story.blocked_reason:
        console.print(f"Blocked: {story.blocked_reason}", markup=False)
    if story.status == StoryStatus.DONE:
        store = StoryStore(path)
        if should_suggest(path, store, config.improve.every_n_stories):
            console.print(
                f"{new_done_count(path, store)} stories delivered since the last improvement. "
                "Run `autodev improve` to review the process and the code.",
                markup=False,
                soft_wrap=True,
            )


@app.command()
def brief(path: PathOpt = Path()) -> None:
    """Technical briefing: define stack and architecture, write ADRs, bootstrap an empty repo."""
    try:
        config = load_config(path)
        _warn_about_hooks(path)
        _run_brief(config, path, make_runtime(config, path), make_io())
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def run(story_id: str, path: PathOpt = Path()) -> None:
    """Implement an approved story (TDD per task), review, QA and merge it into main."""
    try:
        config = load_config(path)
        story = _run_story(config, path, make_runtime(config, path), story_id)
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    _print_final_status(story, config, path)


@app.command()
def resume(
    story_id: str, path: Annotated[Path, typer.Option(help="Project root.")] = Path()
) -> None:
    """Unblock a blocked story so `autodev run` can continue it."""
    try:
        load_config(path)
        store = StoryStore(path)
        story = store.get(story_id)
        story.resume()
    except (ConfigError, InvalidTransition) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    except KeyError as exc:
        err_console.print(f"[red]Error:[/red] Story {story_id} not found.")
        raise typer.Exit(1) from exc
    store.save(story)
    console.print(f"{story.id}: {story.status.value}")


@app.command()
def request(
    text: Annotated[str | None, typer.Argument(help="What to build (asked if omitted).")] = None,
    path: PathOpt = Path(),
) -> None:
    """Take a request through discovery and refinement, then approve it."""
    try:
        config = load_config(path)
        runtime = make_runtime(config, path)
        io = make_io()
        if not text or not text.strip():
            text = io.ask("Describe your request")
        config = _offer_brief(config, path, runtime, io)
        store = StoryStore(path)
        story = asyncio.run(
            handle_request(
                text,
                config=config,
                project_root=path,
                runtime=runtime,
                store=store,
                context=make_context(config, path),
                io=io,
                log_factory=lambda sid: RunLog(path, sid),
            )
        )
        _review_loop(story, config, path, runtime, io, store)
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def discover(story_id: str, path: PathOpt = Path()) -> None:
    """Run discovery and refinement on a DRAFT story (e.g. one suggested by QA), then approve it."""
    try:
        config = load_config(path)
        store = StoryStore(path)
        try:
            story = store.get(story_id)
        except KeyError as exc:
            raise RunError(f"Story {story_id} not found") from exc
        if story.status != StoryStatus.DRAFT:
            raise RunError(
                f"Story {story.id} is {story.status.value}; only draft stories can be discovered"
            )
        runtime = make_runtime(config, path)
        io = make_io()
        config = _offer_brief(config, path, runtime, io)
        asyncio.run(
            discover_and_refine(
                story,
                config=config,
                project_root=path,
                runtime=runtime,
                store=store,
                context=make_context(config, path),
                io=io,
                log=RunLog(path, story.id),
            )
        )
        _review_loop(story, config, path, runtime, io, store)
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc


def _review_loop(
    story: Story,
    config: ProjectConfig,
    path: Path,
    runtime: AgentRuntime,
    io: UserIO,
    store: StoryStore,
) -> None:
    io.show(render_story_summary(story))
    while True:
        choice = io.ask("[a]pprove, [f]eedback, [l]ater").strip().lower()
        if choice == "a":
            approve_story(
                story.id, config=config, project_root=path, repo=GitRepo(path), store=store
            )
            console.print(f"{story.id}: approved")
            if io.ask("Implement now? [y/N]").strip().lower() == "y":
                _print_final_status(_run_story(config, path, runtime, story.id), config, path)
            return
        if choice == "f":
            feedback = io.ask("Feedback")
            asyncio.run(
                refine_story(
                    story,
                    config=config,
                    project_root=path,
                    runtime=runtime,
                    context=make_context(config, path),
                    log=RunLog(path, story.id),
                    feedback=feedback,
                )
            )
            store.save(story)
            io.show(render_story_summary(story))
        elif choice == "l":
            console.print(
                f"Continue later with `autodev approve {story.id}` "
                f'or `autodev refine {story.id} -m "..."`.',
                markup=False,
            )
            return


@app.command()
def approve(story_id: str, path: PathOpt = Path()) -> None:
    """Approve a refined story and commit its domain docs."""
    try:
        config = load_config(path)
        story = approve_story(
            story_id,
            config=config,
            project_root=path,
            repo=GitRepo(path),
            store=StoryStore(path),
        )
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"{story.id}: {story.status.value}")


@app.command()
def refine(
    story_id: str,
    message: Annotated[str, typer.Option("--message", "-m", help="Feedback for the refiner.")],
    path: PathOpt = Path(),
) -> None:
    """Refine a discovered or refined story again, applying feedback."""
    try:
        config = load_config(path)
        store = StoryStore(path)
        try:
            story = store.get(story_id)
        except KeyError as exc:
            raise RunError(f"Story {story_id} not found") from exc
        if story.status not in (StoryStatus.DISCOVERED, StoryStatus.REFINED):
            raise RunError(
                f"Story {story.id} is {story.status.value}; "
                "only discovered or refined stories can be refined"
            )
        asyncio.run(
            refine_story(
                story,
                config=config,
                project_root=path,
                runtime=make_runtime(config, path),
                context=make_context(config, path),
                log=RunLog(path, story.id),
                feedback=message,
            )
        )
        store.save(story)
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(Markdown(render_story_summary(story)))
    console.print(f"{story.id}: {story.status.value}")


@app.command()
def show(story_id: str, path: PathOpt = Path()) -> None:
    """Show a story: user story, acceptance criteria, tasks and status."""
    try:
        load_config(path)
        story = StoryStore(path).get(story_id)
    except ConfigError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
    except KeyError as exc:
        err_console.print(f"[red]Error:[/red] Story {story_id} not found.")
        raise typer.Exit(1) from exc
    console.print(Markdown(render_story_summary(story)))
    console.print(f"{story.id}: {story.status.value}")


@app.command()
def improve(
    process: Annotated[bool, typer.Option("--process", help="Process retrospective only.")] = False,
    code: Annotated[bool, typer.Option("--code", help="Code-health scan only.")] = False,
    lint: Annotated[
        bool, typer.Option("--lint", help="Propose lint rules from the standards.")
    ] = False,
    path: PathOpt = Path(),
) -> None:
    """Review the process (run logs), the code and, with --lint, lint rules; never applies alone."""
    both = not process and not code and not lint
    try:
        config = load_config(path)
        runtime = make_runtime(config, path)
        io = make_io()
        store = StoryStore(path)
        repo = GitRepo(path)
        context = make_context(config, path)
        log = RunLog(path, "improve")
        if process or both:
            try:
                accepted = asyncio.run(
                    run_process_retro(
                        config=config,
                        project_root=path,
                        runtime=runtime,
                        repo=repo,
                        context=context,
                        store=store,
                        io=io,
                        log=log,
                    )
                )
            except NoRunsYet as exc:
                if not both:
                    raise
                console.print(str(exc), markup=False)
            else:
                for p in accepted:
                    if p.kind == "prompt":
                        console.print(f"Wrote .agent/process/{p.target}.md", markup=False)
                    elif p.kind == "standard":
                        console.print(f"Updated .agent/standards/{p.target}", markup=False)
                    else:
                        console.print(
                            f"Config recommendation: {p.target}: {p.content}", markup=False
                        )
                if any(p.kind == "standard" for p in accepted):
                    console.print(
                        "Standards changed. Run `autodev improve --lint` to turn them into "
                        "lint rules.",
                        markup=False,
                    )
        if code or both:
            stories = asyncio.run(
                run_code_scan(
                    config=config,
                    project_root=path,
                    runtime=runtime,
                    repo=repo,
                    context=context,
                    store=store,
                    io=io,
                    log=log,
                )
            )
            for story in stories:
                console.print(f"Created {story.id}: {story.title}", markup=False)
        if lint:
            lint_story = asyncio.run(
                run_lint_proposal(
                    config=config,
                    project_root=path,
                    runtime=runtime,
                    context=context,
                    store=store,
                    io=io,
                    log=log,
                )
            )
            if lint_story is None:
                console.print("No lint story created.", markup=False)
            else:
                console.print(f"Created {lint_story.id}: {lint_story.title}", markup=False)
                console.print(f"Next: autodev discover {lint_story.id}", markup=False)
    except (ConfigError, RunError) as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from exc
