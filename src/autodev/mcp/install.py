import subprocess
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path

from autodev.app.commands import CommandError
from autodev.ports.user_io import UserIO

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]

_REPO = Path(__file__).resolve().parents[3]


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _confirm(io: UserIO, question: str, yes: bool) -> bool:
    if yes:
        return True
    return io.ask(f"{question} [Y/n]").strip().lower() in ("", "y", "yes")


def _register_mcp(io: UserIO, exe: str, yes: bool, runner: Runner, which: Which) -> None:
    command = ["claude", "mcp", "add", "--scope", "user", "autodev", "--", exe, "mcp"]
    manual = " ".join(command)
    if which("claude") is None:
        io.show("`claude` is not on PATH. Register the MCP server yourself with:")
        io.show(manual)
        return
    if runner(["claude", "mcp", "get", "autodev"]).returncode == 0:
        io.show("MCP server autodev: already registered.")
        return
    io.show(f"Will run: {manual}")
    if not _confirm(io, "Register the MCP server?", yes):
        io.show("Skipped. Register it later with:")
        io.show(manual)
        return
    result = runner(command)
    if result.returncode != 0:
        io.show(f"Registration failed: {result.stderr.strip()}")
        io.show("Register it yourself with:")
        io.show(manual)
        return
    io.show("MCP server autodev: registered.")


def _install_skill(io: UserIO, home: Path, yes: bool) -> None:
    source = (
        files("autodev") / "templates" / "claude" / "skills" / "autodev" / "SKILL.md"
    ).read_text()
    target = home / ".claude" / "skills" / "autodev" / "SKILL.md"
    if target.is_file():
        if target.read_text() == source:
            io.show("Claude skill up to date.")
            return
        if not _confirm(io, f"{target} differs from the shipped skill. Overwrite?", yes):
            io.show("Skill left unchanged.")
            return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    io.show(f"Skill written to {target}")


def install_claude(
    io: UserIO, *, yes: bool, home: Path, runner: Runner = run_command, which: Which
) -> None:
    """Register the autodev MCP server and the /autodev skill with Claude Code (user scope)."""
    exe = which("autodev")
    if exe is None:
        raise CommandError(
            "`autodev` is not on PATH. Run `uv tool install --editable "
            f"{_REPO}` first, then try again."
        )
    _register_mcp(io, exe, yes, runner, which)
    _install_skill(io, home, yes)
