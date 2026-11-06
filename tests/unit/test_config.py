from pathlib import Path
from typing import Any

import pytest

from autodev.config import ConfigError, load_config
from autodev.config.models import DEFAULT_TOOLS, Effort
from autodev.domain.role import Role


def write_config(root: Path, content: str) -> None:
    agent = root / ".agent"
    agent.mkdir(exist_ok=True)
    (agent / "config.yaml").write_text(content)


def test_empty_config_file_yields_all_defaults(tmp_path: Path) -> None:
    write_config(tmp_path, "")
    config = load_config(tmp_path)
    assert config.commands.build is None
    assert config.git.main_branch == "main"
    assert config.git.remote is None
    assert config.limits.max_attempts == 3
    assert config.agents.roles == {}


def test_tdd_and_timeout_defaults(tmp_path: Path) -> None:
    write_config(tmp_path, "")
    config = load_config(tmp_path)
    assert config.limits.command_timeout_s == 900
    assert "tests/*" in config.tdd.test_globs
    assert "*.spec.*" in config.tdd.test_globs
    assert len(config.tdd.test_globs) == 10


def test_missing_config_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"config\.yaml"):
        load_config(tmp_path)


def test_invalid_yaml_raises_config_error(tmp_path: Path) -> None:
    write_config(tmp_path, "a: [unclosed")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(tmp_path)


def test_unknown_key_raises_config_error(tmp_path: Path) -> None:
    write_config(tmp_path, "limitz: {max_attempts: 2}\n")
    with pytest.raises(ConfigError, match="limitz"):
        load_config(tmp_path)


def test_invalid_effort_raises_config_error(tmp_path: Path) -> None:
    write_config(tmp_path, "agents:\n  defaults:\n    effort: turbo\n")
    with pytest.raises(ConfigError, match="effort"):
        load_config(tmp_path)


def test_unknown_role_raises_config_error(tmp_path: Path) -> None:
    write_config(tmp_path, "agents:\n  roles:\n    wizard:\n      model: x\n")
    with pytest.raises(ConfigError, match="wizard"):
        load_config(tmp_path)


def test_role_without_override_inherits_defaults(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "agents:\n  defaults:\n    model: m1\n    effort: low\n    max_turns: 7\n",
    )
    profile = load_config(tmp_path).agents.profile_for(Role.QA)
    assert (profile.model, profile.effort, profile.max_turns) == ("m1", Effort.LOW, 7)


def test_partial_role_override_merges_with_defaults(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "agents:\n  defaults:\n    model: m1\n    max_turns: 7\n"
        "  roles:\n    reviewer:\n      effort: max\n      tools: [Read]\n",
    )
    profile = load_config(tmp_path).agents.profile_for(Role.REVIEWER)
    assert profile.model == "m1"
    assert profile.max_turns == 7
    assert profile.effort == Effort.MAX
    assert profile.tools == ["Read"]
    assert profile.prompt is None


def test_builtin_defaults_apply_when_nothing_configured(tmp_path: Path) -> None:
    write_config(tmp_path, "")
    profile = load_config(tmp_path).agents.profile_for(Role.DISCOVERY)
    assert profile.model == "claude-opus-5-5"
    assert profile.effort == Effort.HIGH
    assert profile.max_turns == 50
    assert profile.tools == DEFAULT_TOOLS
    assert profile.max_budget_usd is None
    assert profile.prompt is None


def test_max_budget_and_default_tools_resolution(tmp_path: Path) -> None:
    write_config(
        tmp_path,
        "agents:\n  defaults:\n    max_budget_usd: 2.5\n    tools: [Read]\n"
        "  roles:\n    qa:\n      max_budget_usd: 1\n      tools: []\n"
        "    reviewer:\n      tools: [Grep]\n",
    )
    agents = load_config(tmp_path).agents
    qa = agents.profile_for(Role.QA)
    assert (qa.max_budget_usd, qa.tools) == (1, [])
    assert agents.profile_for(Role.REVIEWER).tools == ["Grep"]
    other = agents.profile_for(Role.MERGER)
    assert (other.max_budget_usd, other.tools) == (2.5, ["Read"])
    write_config(tmp_path, "agents:\n  defaults:\n    max_budget_usd: 0\n")
    with pytest.raises(ConfigError, match="max_budget_usd"):
        load_config(tmp_path)


def test_template_config_is_valid_and_has_mixed_defaults(tmp_path: Path) -> None:
    from importlib.resources import files

    text: Any = (files("autodev") / "templates" / "agent" / "config.yaml").read_text()
    write_config(tmp_path, text)
    agents = load_config(tmp_path).agents
    implementer = agents.profile_for(Role.IMPLEMENTER)
    assert (implementer.model, implementer.effort) == ("claude-sonnet-5-5", Effort.MEDIUM)
    architect = agents.profile_for(Role.ARCHITECT)
    assert (architect.model, architect.effort) == ("claude-opus-5-5", Effort.XHIGH)
    assert agents.profile_for(Role.REVIEWER).tools == ["Read", "Grep", "Glob", "Bash"]
    assert load_config(tmp_path).limits.command_timeout_s == 900
    assert "*_test.go" in load_config(tmp_path).tdd.test_globs
