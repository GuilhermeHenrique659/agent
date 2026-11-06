from pathlib import Path

from typer.testing import CliRunner

from autodev.cli import app
from autodev.domain.story import Story
from autodev.pipeline.state_store import StoryStore

runner = CliRunner()


def test_init_creates_agent_folder_with_template(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0
    agent = tmp_path / ".agent"
    assert (agent / "config.yaml").is_file()
    assert (agent / "knowledge" / "README.md").is_file()
    assert (agent / "domain" / "glossary.md").is_file()
    assert (agent / "architecture" / "adr" / "0000-template.md").is_file()
    assert (agent / "backlog" / ".gitkeep").is_file()
    assert (agent / "runs" / ".gitkeep").is_file()


def test_init_refuses_existing_agent_folder(tmp_path: Path) -> None:
    (tmp_path / ".agent").mkdir()
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 1
    assert list((tmp_path / ".agent").iterdir()) == []


def test_status_without_config_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 1


def test_status_lists_stories(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    StoryStore(tmp_path).save(Story("S-0001", "First story"))
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 0
    assert "S-0001" in result.output
    assert "First story" in result.output
    assert "draft" in result.output


def test_status_empty_backlog(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    result = runner.invoke(app, ["status", str(tmp_path)])
    assert result.exit_code == 0
    assert "No stories yet." in result.output


def write_commands(root: Path, build: str, test: str, lint: str) -> None:
    runner.invoke(app, ["init", str(root)])
    (root / ".agent" / "config.yaml").write_text(
        f"commands:\n  build: {build!r}\n  test: {test!r}\n  lint: {lint!r}\n"
    )


def test_check_passes_when_all_gates_ok(tmp_path: Path) -> None:
    write_commands(tmp_path, "exit 0", "exit 0", "exit 0")
    result = runner.invoke(app, ["check", str(tmp_path)])
    assert result.exit_code == 0
    assert "passed" in result.output


def test_check_fails_and_shows_output(tmp_path: Path) -> None:
    write_commands(tmp_path, "exit 0", "echo boom-output; exit 1", "exit 0")
    result = runner.invoke(app, ["check", str(tmp_path)])
    assert result.exit_code == 1
    assert "failed" in result.output
    assert "boom-output" in result.output


def test_check_without_config_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", str(tmp_path)])
    assert result.exit_code == 1
