import asyncio
import shutil
from collections.abc import Coroutine
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from autodev.adapters.claude_settings import hooks_warning, project_hook_events
from autodev.adapters.console_io import ConsoleIO
from autodev.app import commands
from autodev.app.commands import CommandError
from autodev.app.runtime import make_runtime as make_runtime
from autodev.mcp.install import install_claude
from autodev.onboarding.scaffold import create_agent_dir
from autodev.ports.user_io import UserIO

app = typer.Typer(help="Orchestrate AI agents to build software.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

PathArg = Annotated[Path, typer.Argument(help="Project root (default: current directory).")]
PathOpt = Annotated[Path, typer.Option(help="Project root.")]


def make_io() -> UserIO:
    """Factory for the user IO; tests monkeypatch it."""
    return ConsoleIO()


def _execute(command: Coroutine[Any, Any, None]) -> None:
    try:
        asyncio.run(command)
    except CommandError as exc:
        err_console.print(f"[red]Error:[/red] {exc}", markup=True, highlight=False)
        raise typer.Exit(1) from exc


@app.command()
def init(path: PathArg = Path()) -> None:
    """Create the .agent/ folder from the template."""
    target = path / ".agent"
    if target.exists():
        err_console.print(f"[red]Error:[/red] {target} already exists.")
        raise typer.Exit(1)
    create_agent_dir(path)
    console.print(f"Created {target}")
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
    _execute(
        commands.onboard(
            path,
            make_io(),
            yes=yes,
            brief=not no_brief,
            home=Path.home(),
            make_runtime=make_runtime,
        )
    )


@app.command()
def doctor(path: PathOpt = Path()) -> None:
    """Check that the agent runtime can reach the model (one cheap call)."""
    _execute(commands.doctor(path, make_io(), make_runtime=make_runtime))


@app.command()
def status(path: PathArg = Path()) -> None:
    """List the stories in the backlog."""
    _execute(commands.status(path, make_io()))


@app.command()
def check(path: PathArg = Path()) -> None:
    """Run the quality gates (build, test, lint)."""
    _execute(commands.check(path, make_io()))


@app.command()
def brief(path: PathOpt = Path()) -> None:
    """Technical briefing: define stack and architecture, write ADRs, bootstrap an empty repo."""
    _execute(commands.brief(path, make_io(), make_runtime=make_runtime))


@app.command()
def run(story_id: str, path: PathOpt = Path()) -> None:
    """Implement an approved story (TDD per task), review, QA and merge it into main."""
    _execute(commands.run(path, make_io(), story_id, make_runtime=make_runtime))


@app.command()
def resume(story_id: str, path: PathOpt = Path()) -> None:
    """Unblock a blocked story so `autodev run` can continue it."""
    _execute(commands.resume(path, make_io(), story_id))


@app.command()
def request(
    text: Annotated[str | None, typer.Argument(help="What to build (asked if omitted).")] = None,
    path: PathOpt = Path(),
) -> None:
    """Take a request through discovery and refinement, then approve it."""
    _execute(commands.request(path, make_io(), text, make_runtime=make_runtime))


@app.command()
def discover(story_id: str, path: PathOpt = Path()) -> None:
    """Run discovery and refinement on a DRAFT story (e.g. one suggested by QA), then approve it."""
    _execute(commands.discover(path, make_io(), story_id, make_runtime=make_runtime))


@app.command()
def approve(story_id: str, path: PathOpt = Path()) -> None:
    """Approve a refined story and commit its domain docs."""
    _execute(commands.approve(path, make_io(), story_id))


@app.command()
def refine(
    story_id: str,
    message: Annotated[str, typer.Option("--message", "-m", help="Feedback for the refiner.")],
    path: PathOpt = Path(),
) -> None:
    """Refine a discovered or refined story again, applying feedback."""
    _execute(commands.refine(path, make_io(), story_id, message, make_runtime=make_runtime))


@app.command()
def show(story_id: str, path: PathOpt = Path()) -> None:
    """Show a story: user story, acceptance criteria, tasks and status."""
    _execute(commands.show(path, make_io(), story_id))


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
    _execute(
        commands.improve(
            path, make_io(), process=process, code=code, lint=lint, make_runtime=make_runtime
        )
    )


@app.command()
def mcp() -> None:
    """Run autodev as an MCP server on stdio (for use from Claude Code)."""
    from autodev.mcp.server import build_server
    from autodev.mcp.sessions import SessionManager

    build_server(SessionManager()).run()


@app.command("install-claude")
def install_claude_command(
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept every default without asking.")
    ] = False,
) -> None:
    """Register the autodev MCP server and the /autodev skill with Claude Code."""

    async def install() -> None:
        install_claude(make_io(), yes=yes, home=Path.home(), which=shutil.which)

    _execute(install())
