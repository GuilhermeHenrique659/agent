from pathlib import Path

import pytest

from autodev.config import ConfigError, load_config
from autodev.pipeline import config_edit
from autodev.pipeline.config_edit import set_commands
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
