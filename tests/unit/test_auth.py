import json
import stat
from pathlib import Path

from autodev.onboarding.auth import minimal_auth_settings, write_auth_settings


def write_home(home: Path, data: object) -> None:
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(data))


def test_minimal_settings_from_home_filters_env(tmp_path: Path) -> None:
    write_home(
        tmp_path,
        {
            "apiKeyHelper": "/bin/helper",
            "hooks": {"Stop": []},
            "permissions": {"allow": ["x"]},
            "env": {
                "ANTHROPIC_BASE_URL": "https://x",
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "300000",
                "OTHER": "no",
            },
        },
    )
    assert minimal_auth_settings(tmp_path) == {
        "apiKeyHelper": "/bin/helper",
        "env": {
            "ANTHROPIC_BASE_URL": "https://x",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "300000",
        },
    }


def test_env_omitted_when_empty(tmp_path: Path) -> None:
    write_home(tmp_path, {"apiKeyHelper": "h", "env": {"OTHER": "x"}})
    assert minimal_auth_settings(tmp_path) == {"apiKeyHelper": "h"}


def test_none_without_api_key_helper_or_file(tmp_path: Path) -> None:
    assert minimal_auth_settings(tmp_path) is None
    write_home(tmp_path, {"env": {"ANTHROPIC_API_KEY": "k"}})
    assert minimal_auth_settings(tmp_path) is None
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    assert minimal_auth_settings(tmp_path) is None


def test_write_auth_settings_permissions(tmp_path: Path) -> None:
    (tmp_path / ".agent").mkdir()
    path = write_auth_settings(tmp_path, {"apiKeyHelper": "h"})
    assert path == tmp_path / ".agent" / "auth.json"
    assert json.loads(path.read_text()) == {"apiKeyHelper": "h"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
