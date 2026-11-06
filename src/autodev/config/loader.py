from pathlib import Path

import yaml
from pydantic import ValidationError

from autodev.config.models import ProjectConfig


class ConfigError(Exception):
    pass


def load_config(project_root: Path) -> ProjectConfig:
    path = project_root / ".agent" / "config.yaml"
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    try:
        return ProjectConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"Invalid config in {path}:\n{exc}") from exc
