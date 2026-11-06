import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from autodev.app.commands import CommandError
from autodev.mcp.install import install_claude
from tests.fakes import FakeIO

EXE = "/usr/local/bin/autodev"
ADD = ["claude", "mcp", "add", "--scope", "user", "autodev", "--", EXE, "mcp"]
ADD_TEXT = f"claude mcp add --scope user autodev -- {EXE} mcp"


class FakeRunner:
    def __init__(self, get_code: int = 1, add_code: int = 0, add_stderr: str = "") -> None:
        self.codes = {"get": get_code, "add": add_code}
        self.add_stderr = add_stderr
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        action = command[2]
        stderr = self.add_stderr if action == "add" else ""
        return subprocess.CompletedProcess(command, self.codes[action], "", stderr)


def which_all(name: str) -> str | None:
    return {"autodev": EXE, "claude": "/usr/local/bin/claude"}.get(name)


def which_without_claude(name: str) -> str | None:
    return EXE if name == "autodev" else None


def skill_source() -> str:
    return (
        files("autodev") / "templates" / "claude" / "skills" / "autodev" / "SKILL.md"
    ).read_text()


def skill_target(home: Path) -> Path:
    return home / ".claude" / "skills" / "autodev" / "SKILL.md"


def text(io: FakeIO) -> str:
    return "\n".join(io.shown)


def test_registers_mcp_and_writes_skill(tmp_path: Path) -> None:
    runner = FakeRunner()
    io = FakeIO("")
    install_claude(io, yes=False, home=tmp_path, runner=runner, which=which_all)
    assert runner.calls == [["claude", "mcp", "get", "autodev"], ADD]
    assert ADD_TEXT in text(io)
    assert skill_target(tmp_path).read_text() == skill_source()
    assert len(io.prompts) == 1


def test_skips_when_already_registered_and_skill_up_to_date(tmp_path: Path) -> None:
    target = skill_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(skill_source())
    runner = FakeRunner(get_code=0)
    io = FakeIO()
    install_claude(io, yes=False, home=tmp_path, runner=runner, which=which_all)
    assert runner.calls == [["claude", "mcp", "get", "autodev"]]
    assert "already registered" in text(io)
    assert "skill up to date" in text(io)
    assert io.prompts == []


def test_asks_before_overwriting_different_skill(tmp_path: Path) -> None:
    target = skill_target(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("mine")
    install_claude(FakeIO("n"), yes=False, home=tmp_path, runner=FakeRunner(0), which=which_all)
    assert target.read_text() == "mine"
    install_claude(FakeIO("y"), yes=False, home=tmp_path, runner=FakeRunner(0), which=which_all)
    assert target.read_text() == skill_source()
    target.write_text("mine")
    io = FakeIO()
    install_claude(io, yes=True, home=tmp_path, runner=FakeRunner(0), which=which_all)
    assert target.read_text() == skill_source()
    assert io.prompts == []


def test_missing_claude_prints_manual_command(tmp_path: Path) -> None:
    runner = FakeRunner()
    io = FakeIO()
    install_claude(io, yes=True, home=tmp_path, runner=runner, which=which_without_claude)
    assert runner.calls == []
    assert ADD_TEXT in text(io)
    assert skill_target(tmp_path).is_file()


def test_failed_registration_prints_stderr_and_manual_command(tmp_path: Path) -> None:
    runner = FakeRunner(add_code=1, add_stderr="scope exploded")
    io = FakeIO()
    install_claude(io, yes=True, home=tmp_path, runner=runner, which=which_all)
    assert "scope exploded" in text(io)
    assert ADD_TEXT in text(io)
    assert skill_target(tmp_path).is_file()


def test_missing_autodev_executable_fails(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="uv tool install --editable"):
        install_claude(
            FakeIO(), yes=True, home=tmp_path, runner=FakeRunner(), which=lambda name: None
        )
    assert not skill_target(tmp_path).exists()
