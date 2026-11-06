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
