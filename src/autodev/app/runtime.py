from pathlib import Path

from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime
from autodev.config import ConfigError
from autodev.config.models import ProjectConfig
from autodev.ports.agent_runtime import AgentRuntime


def make_runtime(config: ProjectConfig, root: Path) -> AgentRuntime:
    """Factory for the agent runtime; tests inject their own."""
    settings = config.runtime.settings
    if settings is not None:
        settings_path = Path(settings).expanduser()
        if not settings_path.is_absolute():
            settings_path = root / ".agent" / settings_path
        if not settings_path.is_file():
            raise ConfigError(f"runtime.settings file not found: {settings_path}")
        settings = str(settings_path)
    return ClaudeSdkRuntime(settings=settings, setting_sources=config.runtime.setting_sources)
