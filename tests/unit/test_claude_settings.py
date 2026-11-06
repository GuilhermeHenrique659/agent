import json
from pathlib import Path

from autodev.adapters.claude_settings import project_hook_events


def write_settings(root: Path, content: str) -> None:
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.json").write_text(content)


def test_hook_events_listed(tmp_path: Path) -> None:
    hooks = {"Stop": [], "PreToolUse": [{"matcher": "Bash"}]}
    write_settings(tmp_path, json.dumps({"hooks": hooks, "model": "x"}))
    assert project_hook_events(tmp_path) == ["PreToolUse", "Stop"]


def test_no_settings_or_no_hooks_or_invalid_json_returns_empty(tmp_path: Path) -> None:
    assert project_hook_events(tmp_path) == []
    write_settings(tmp_path, json.dumps({"model": "x"}))
    assert project_hook_events(tmp_path) == []
    write_settings(tmp_path, json.dumps({"hooks": []}))
    assert project_hook_events(tmp_path) == []
    write_settings(tmp_path, "{not json")
    assert project_hook_events(tmp_path) == []
