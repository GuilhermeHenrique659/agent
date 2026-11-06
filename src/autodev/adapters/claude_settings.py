import json
from pathlib import Path


def project_hook_events(project_root: Path) -> list[str]:
    """Hook events defined in the project's .claude/settings.json (empty if none or unreadable)."""
    path = project_root / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return sorted(hooks) if isinstance(hooks, dict) else []


def hooks_warning(events: list[str]) -> str:
    return (
        f"Warning: this project defines Claude Code hooks ({', '.join(events)}) in "
        ".claude/settings.json. They also run inside autodev agent sessions; make sure "
        "none of them commit, push or switch branches."
    )
