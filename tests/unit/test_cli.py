import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autodev import cli
from autodev.adapters.git import GitRepo
from autodev.cli import app
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task
from autodev.pipeline.outputs import (
    DiscoveryResult,
    DiscoveryTurn,
    GlossaryEntry,
    ImplementerOutput,
    RefinedTask,
    RefinementOutput,
    ReviewOutput,
    TestWriterOutput,
)
from autodev.pipeline.state_store import StoryStore
from tests.fakes import FakeAgentRuntime, FakeIO, Step
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


def refined_output(title: str = "Refined") -> RefinementOutput:
    return RefinementOutput(
        title=title,
        user_story="As a buyer, I want to pay",
        acceptance_criteria=["Given a cart When paid Then done"],
        tasks=[RefinedTask(title="first")],
    )


def discovery_done() -> Step:
    result = DiscoveryResult(
        title="Checkout",
        problem="Cannot pay",
        glossary=[GlossaryEntry(term="Cart", definition="Items")],
    )
    return Step(DiscoveryTurn(message="Clear enough", done=True, result=result))


def setup_git_project(root: Path) -> None:
    runner.invoke(app, ["init", str(root)])
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    repo.commit_all("initial")


def patch_factories(monkeypatch: pytest.MonkeyPatch, fake: FakeAgentRuntime, io: FakeIO) -> None:
    monkeypatch.setattr(cli, "make_runtime", lambda config, root: fake)
    monkeypatch.setattr(cli, "make_io", lambda: io)


def test_request_flow_approve_and_skip_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup_git_project(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.DISCOVERY, discovery_done())
    fake.script(Role.REFINER, Step(refined_output()))
    io = FakeIO("a", "n")
    patch_factories(monkeypatch, fake, io)
    result = runner.invoke(app, ["request", "Pay with card", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    story = StoryStore(tmp_path).get("S-0001")
    assert story.status == StoryStatus.APPROVED
    assert any("As a buyer, I want to pay" in text and "first" in text for text in io.shown)
    assert io.prompts == ["[a]pprove, [f]eedback, [l]ater", "Implement now? [y/N]"]
    log = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    assert log == "docs(S-0001): discovery and refinement"
    assert "approved" in result.output


def test_request_feedback_then_later(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_git_project(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.DISCOVERY, discovery_done())
    fake.script(Role.REFINER, Step(refined_output()), Step(refined_output("Second")))
    io = FakeIO("x", "f", "split it", "l")
    patch_factories(monkeypatch, fake, io)
    result = runner.invoke(app, ["request", "Pay with card", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "autodev approve S-0001" in result.output
    story = StoryStore(tmp_path).get("S-0001")
    assert story.status == StoryStatus.REFINED
    assert story.title == "Second"
    assert "split it" in fake.requests_for(Role.REFINER)[1].prompt
    assert len(io.shown) == 3  # discovery turn + two summaries


def test_request_without_text_asks_for_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_git_project(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.DISCOVERY, discovery_done())
    fake.script(Role.REFINER, Step(refined_output()))
    io = FakeIO("Pay with card", "l")
    patch_factories(monkeypatch, fake, io)
    result = runner.invoke(app, ["request", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert StoryStore(tmp_path).get("S-0001").description.count("Pay with card") >= 1
    assert "Pay with card" in fake.messages_for(Role.DISCOVERY)[0]


def refined_story(root: Path, status: StoryStatus = StoryStatus.REFINED) -> None:
    setup_git_project(root)
    story = Story(
        "S-0001",
        "Pay",
        description="## User story\nAs a buyer, I want to pay\n\n## Request\nraw\n",
        status=status,
        acceptance_criteria=["Given a cart When paid Then done"],
        tasks=[Task("T1", "first")],
    )
    StoryStore(root).save(story)


def test_approve_command(tmp_path: Path) -> None:
    refined_story(tmp_path)
    result = runner.invoke(app, ["approve", "S-0001", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "approved" in result.output
    assert StoryStore(tmp_path).get("S-0001").status == StoryStatus.APPROVED
    again = runner.invoke(app, ["approve", "S-0001", "--path", str(tmp_path)])
    assert again.exit_code == 1
    missing = runner.invoke(app, ["approve", "S-9999", "--path", str(tmp_path)])
    assert missing.exit_code == 1


def test_refine_command_with_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    refined_story(tmp_path)
    fake = FakeAgentRuntime()
    fake.script(Role.REFINER, Step(refined_output("Better")))
    patch_factories(monkeypatch, fake, FakeIO())
    result = runner.invoke(
        app, ["refine", "S-0001", "-m", "make it smaller", "--path", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "make it smaller" in fake.requests[0].prompt
    assert StoryStore(tmp_path).get("S-0001").title == "Better"
    StoryStore(tmp_path).save(Story("S-0002", "x", status=StoryStatus.APPROVED, description="d"))
    bad = runner.invoke(app, ["refine", "S-0002", "-m", "x", "--path", str(tmp_path)])
    assert bad.exit_code == 1


def test_show_command(tmp_path: Path) -> None:
    refined_story(tmp_path)
    result = runner.invoke(app, ["show", "S-0001", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    for expected in ("Pay", "As a buyer", "Given a cart", "T1", "refined"):
        assert expected in result.output
    missing = runner.invoke(app, ["show", "S-9999", "--path", str(tmp_path)])
    assert missing.exit_code == 1
