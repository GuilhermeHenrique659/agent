import shutil
from importlib.resources import as_file, files
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from autodev.config import ConfigError, load_config
from autodev.gates.runner import run_gates
from autodev.pipeline.state_store import StoryStore

app = typer.Typer(help="Orchestrate AI agents to build software.", no_args_is_help=True)
console = Console()
err_console = Console(stderr=True)

PathArg = Annotated[Path, typer.Argument(help="Project root (default: current directory).")]


@app.command()
def init(path: PathArg = Path()) -> None:
    """Create the .agent/ folder from the template."""
    target = path / ".agent"
    if target.exists():
        err_console.print(f"[red]Error:[/red] {target} already exists.")
        raise typer.Exit(1)
    with as_file(files("autodev") / "templates" / "agent") as template:
        shutil.copytree(template, target)
    console.print(f"Created {target}")


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
