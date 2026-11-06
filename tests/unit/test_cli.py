import shlex
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autodev import cli
from autodev.adapters.git import GitRepo
from autodev.cli import app
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task
from autodev.pipeline.outputs import ImplementerOutput, ReviewOutput, TestWriterOutput
from autodev.pipeline.state_store import StoryStore
from tests.fakes import FakeAgentRuntime, Step
from tests.integration.conftest import git
from tests.unit.test_tdd import CHECK, IMPL_BAD, IMPL_GOOD

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
    ignore = (agent / ".gitignore").read_text().split()
    assert "backlog/" in ignore and "runs/" in ignore


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


def test_resume_blocked_story(tmp_path: Path) -> None:
    runner.invoke(app, ["init", str(tmp_path)])
    story = Story("S-0001", "First story", status=StoryStatus.IN_PROGRESS)
    story.block("stuck")
    store = StoryStore(tmp_path)
    store.save(story)
    result = runner.invoke(app, ["resume", "S-0001", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "in_progress" in result.output
    assert store.get("S-0001").status == StoryStatus.IN_PROGRESS
    again = runner.invoke(app, ["resume", "S-0001", "--path", str(tmp_path)])
    assert again.exit_code == 1


def setup_runnable_project(root: Path) -> None:
    runner.invoke(app, ["init", str(root)])
    command = f"{shlex.quote(sys.executable)} -B tests/check.py"
    (root / ".agent" / "config.yaml").write_text(f"commands:\n  test: {command!r}\n")
    (root / "impl.py").write_text(IMPL_BAD)
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    story = Story(
        "S-0001",
        "Say two",
        status=StoryStatus.APPROVED,
        acceptance_criteria=["value() is 2"],
        tasks=[Task("T1", "Say two")],
    )
    StoryStore(root).save(story)
    repo.commit_all("initial")


def test_run_prints_final_status_with_fake_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_runnable_project(tmp_path)

    def write_tests(workdir: Path) -> None:
        (workdir / "tests").mkdir(exist_ok=True)
        (workdir / "tests" / "check.py").write_text(CHECK)

    def write_impl(workdir: Path) -> None:
        (workdir / "impl.py").write_text(IMPL_GOOD)

    fake = FakeAgentRuntime()
    fake.script(
        Role.TEST_WRITER,
        Step(TestWriterOutput(summary="t", test_files=["tests/check.py"]), write_tests),
    )
    fake.script(Role.IMPLEMENTER, Step(ImplementerOutput(summary="i"), write_impl))
    fake.script(Role.REVIEWER, Step(ReviewOutput(approved=True, summary="ok", findings=[])))
    monkeypatch.setattr(cli, "make_runtime", lambda config, root: fake)
    result = runner.invoke(app, ["run", "S-0001", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "in_qa" in result.output
    assert StoryStore(tmp_path).get("S-0001").status == StoryStatus.IN_QA


def test_run_unknown_story_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_runnable_project(tmp_path)
    monkeypatch.setattr(cli, "make_runtime", lambda config, root: FakeAgentRuntime())
    result = runner.invoke(app, ["run", "S-9999", "--path", str(tmp_path)])
    assert result.exit_code == 1
    assert "S-9999" in result.output


def test_make_runtime_resolves_settings_relative_to_agent_dir(tmp_path: Path) -> None:
    from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime
    from autodev.config.models import ProjectConfig, RuntimeConfig

    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "auth.json").write_text("{}")
    config = ProjectConfig(
        runtime=RuntimeConfig(settings="auth.json", setting_sources=["user", "project"])
    )
    runtime = cli.make_runtime(config, tmp_path)
    assert isinstance(runtime, ClaudeSdkRuntime)
    assert runtime.settings == str(tmp_path / ".agent" / "auth.json")
    assert runtime.setting_sources == ["user", "project"]


def test_make_runtime_with_missing_settings_file_raises_config_error(tmp_path: Path) -> None:
    from autodev.config import ConfigError
    from autodev.config.models import ProjectConfig, RuntimeConfig

    config = ProjectConfig(runtime=RuntimeConfig(settings="missing.json"))
    with pytest.raises(ConfigError, match=r"missing\.json"):
        cli.make_runtime(config, tmp_path)
