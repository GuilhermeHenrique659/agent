from pathlib import Path

import pytest

from autodev.config import ConfigError, load_config
from autodev.pipeline import config_edit
from autodev.pipeline.config_edit import set_commands, set_value
from autodev.pipeline.outputs import Commands

CONFIG = """# header comment
commands:
  build: null  # build it
  test: null   # test it
  lint: null
git:
  main_branch: main
  remote: null
"""


def write(root: Path, text: str = CONFIG) -> Path:
    path = root / ".agent" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_sets_commands_preserving_comments_and_other_lines(tmp_path: Path) -> None:
    path = write(tmp_path)
    set_commands(tmp_path, Commands(build='make "all"', test="pytest", lint="ruff check ."))
    assert path.read_text() == (
        "# header comment\n"
        "commands:\n"
        '  build: "make \\"all\\""  # build it\n'
        '  test: "pytest"   # test it\n'
        '  lint: "ruff check ."\n'
        "git:\n  main_branch: main\n  remote: null\n"
    )
    config = load_config(tmp_path)
    assert config.commands.build == 'make "all"'
    assert config.commands.test == "pytest"


def test_writes_null_for_missing_command(tmp_path: Path) -> None:
    path = write(tmp_path, CONFIG.replace("build: null", 'build: "x"'))
    set_commands(tmp_path, Commands(test="pytest"))
    assert "  build: null  # build it\n" in path.read_text()
    assert load_config(tmp_path).commands.build is None


def test_inserts_missing_key_in_block(tmp_path: Path) -> None:
    path = write(tmp_path, "commands:\n  test: null\ngit:\n  remote: null\n")
    set_commands(tmp_path, Commands(build="make", test="pytest", lint="ruff"))
    assert path.read_text().startswith("commands:\n")
    config = load_config(tmp_path)
    assert (config.commands.build, config.commands.test, config.commands.lint) == (
        "make",
        "pytest",
        "ruff",
    )
    assert path.read_text().endswith("git:\n  remote: null\n")


def test_restores_file_when_result_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path)

    def boom(root: Path) -> None:
        raise ConfigError("bad")

    monkeypatch.setattr(config_edit, "load_config", boom)
    with pytest.raises(ConfigError):
        set_commands(tmp_path, Commands(test="pytest"))
    assert path.read_text() == CONFIG


FULL = """commands:
  test: null
git:
  main_branch: main
  remote: null  # push target
runtime:
  settings: null
agents:
  defaults:
    model: claude-opus-5-5  # model
    # max_budget_usd: 5.0
  roles:
    qa:
      effort: high
"""


def test_set_value_nested_keys_preserving_comments(tmp_path: Path) -> None:
    path = write(tmp_path, FULL)
    set_value(tmp_path, "git.remote", "origin")
    set_value(tmp_path, "agents.defaults.model", "claude-sonnet-5-5")
    text = path.read_text()
    assert '  remote: "origin"  # push target\n' in text
    assert '    model: "claude-sonnet-5-5"  # model\n' in text
    config = load_config(tmp_path)
    assert config.git.remote == "origin"
    assert config.agents.defaults.model == "claude-sonnet-5-5"


def test_set_value_inserts_missing_key_ignoring_commented_line(tmp_path: Path) -> None:
    path = write(tmp_path, FULL)
    set_value(tmp_path, "agents.defaults.max_budget_usd", 5.0)
    text = path.read_text()
    assert "    # max_budget_usd: 5.0\n" in text
    assert "    max_budget_usd: 5.0\n  roles:\n" in text
    assert load_config(tmp_path).agents.defaults.max_budget_usd == 5.0


def test_set_value_creates_missing_parent_blocks(tmp_path: Path) -> None:
    path = write(tmp_path, "commands:\n  test: null\n")
    set_value(tmp_path, "context.paths", ["README.md"])
    set_value(tmp_path, "agents.defaults.max_turns", 7)
    assert path.read_text() == (
        'commands:\n  test: null\ncontext:\n  paths: ["README.md"]\n'
        "agents:\n  defaults:\n    max_turns: 7\n"
    )
    assert load_config(tmp_path).agents.defaults.max_turns == 7


def test_set_value_list_and_number_and_null(tmp_path: Path) -> None:
    path = write(tmp_path, FULL)
    set_value(tmp_path, "context.paths", ["README.md", "docs/"])
    set_value(tmp_path, "limits.max_attempts", 4)
    set_value(tmp_path, "agents.roles.qa.effort", None)
    text = path.read_text()
    assert 'paths: ["README.md", "docs/"]' in text
    assert "  max_attempts: 4\n" in text
    assert "      effort: null\n" in text
    config = load_config(tmp_path)
    assert config.context.paths == ["README.md", "docs/"]
    assert config.limits.max_attempts == 4


def test_set_value_restores_on_invalid(tmp_path: Path) -> None:
    path = write(tmp_path, FULL)
    with pytest.raises(ConfigError):
        set_value(tmp_path, "limits.max_attempts", "lots")
    assert path.read_text() == FULL


def test_set_commands_writes_format(tmp_path: Path) -> None:
    path = write(tmp_path)
    set_commands(tmp_path, Commands(test="pytest", format="ruff format ."))
    assert load_config(tmp_path).commands.format == "ruff format ."
    assert '  format: "ruff format ."\n' in path.read_text()
    set_commands(tmp_path, Commands(test="pytest"))
    assert load_config(tmp_path).commands.format is None
