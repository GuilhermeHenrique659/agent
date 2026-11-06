import json
from pathlib import Path
from typing import Any

_KEPT_ENV_EXACT = "CLAUDE_CODE_API_KEY_HELPER_TTL_MS"
_KEPT_ENV_PREFIX = "ANTHROPIC_"


def minimal_auth_settings(home: Path) -> dict[str, Any] | None:
    """Only what authentication needs from ~/.claude/settings.json (None if no apiKeyHelper)."""
    try:
        data = json.loads((home / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("apiKeyHelper"):
        return None
    result: dict[str, Any] = {"apiKeyHelper": data["apiKeyHelper"]}
    env = data.get("env")
    if isinstance(env, dict):
        kept = {
            k: v for k, v in env.items() if k.startswith(_KEPT_ENV_PREFIX) or k == _KEPT_ENV_EXACT
        }
        if kept:
            result["env"] = kept
    return result


def write_auth_settings(project_root: Path, data: dict[str, Any]) -> Path:
    path = project_root / ".agent" / "auth.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
    return path
