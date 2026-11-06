from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from autodev.domain.role import Role

_STRICT = ConfigDict(extra="forbid")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TURNS = 50
DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


Skills = list[str] | Literal["all"]  # a list of skill names, or every discovered skill


class AgentProfile(BaseModel):
    model_config = _STRICT

    model: str = Field(min_length=1)
    effort: Effort
    max_turns: int = Field(ge=1)
    tools: list[str]
    max_budget_usd: float | None
    prompt: str | None
    skills: Skills | None


class AgentOverride(BaseModel):
    model_config = _STRICT

    model: str | None = Field(default=None, min_length=1)
    effort: Effort | None = None
    max_turns: int | None = Field(default=None, ge=1)
    tools: list[str] | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    prompt: str | None = None
    skills: Skills | None = None


class AgentsConfig(BaseModel):
    model_config = _STRICT

    defaults: AgentOverride = AgentOverride()
    roles: dict[Role, AgentOverride] = {}

    def profile_for(self, role: Role) -> AgentProfile:
        specific = self.roles.get(role, AgentOverride())
        d = self.defaults
        # model, effort and max_turns are never falsy once validated, so `or` is safe;
        # tools may be [] (no tools), which must not fall through to the default.
        return AgentProfile(
            model=specific.model or d.model or DEFAULT_MODEL,
            effort=specific.effort or d.effort or Effort.HIGH,
            max_turns=specific.max_turns or d.max_turns or DEFAULT_MAX_TURNS,
            tools=list(
                specific.tools
                if specific.tools is not None
                else d.tools
                if d.tools is not None
                else DEFAULT_TOOLS
            ),
            max_budget_usd=(
                specific.max_budget_usd if specific.max_budget_usd is not None else d.max_budget_usd
            ),
            prompt=specific.prompt if specific.prompt is not None else d.prompt,
            skills=specific.skills if specific.skills is not None else d.skills,
        )


class CommandsConfig(BaseModel):
    model_config = _STRICT

    build: str | None = None
    test: str | None = None
    lint: str | None = None


class GitConfig(BaseModel):
    model_config = _STRICT

    main_branch: str = "main"
    remote: str | None = None


class LimitsConfig(BaseModel):
    model_config = _STRICT

    max_attempts: int = Field(default=3, ge=1)
    command_timeout_s: int = Field(default=900, ge=1)
    max_discovery_turns: int = Field(default=20, ge=1)


DEFAULT_TEST_GLOBS = [
    "tests/*",
    "test/*",
    "*/tests/*",
    "*/test/*",
    "test_*.py",
    "*/test_*.py",
    "*_test.py",
    "*_test.go",
    "*.test.*",
    "*.spec.*",
]


class TddConfig(BaseModel):
    model_config = _STRICT

    test_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))


SettingSource = Literal["user", "project", "local"]


def _default_setting_sources() -> list[SettingSource]:
    return ["project"]


class RuntimeConfig(BaseModel):
    """How agent sessions load Claude Code settings (e.g. authentication)."""

    model_config = _STRICT

    setting_sources: list[SettingSource] = Field(default_factory=_default_setting_sources)
    settings: str | None = None  # extra settings JSON; relative to .agent/, absolute or ~


class ImproveConfig(BaseModel):
    model_config = _STRICT

    every_n_stories: int = Field(default=5, ge=1)


class ProjectConfig(BaseModel):
    model_config = _STRICT

    commands: CommandsConfig = CommandsConfig()
    git: GitConfig = GitConfig()
    limits: LimitsConfig = LimitsConfig()
    tdd: TddConfig = TddConfig()
    agents: AgentsConfig = AgentsConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    improve: ImproveConfig = ImproveConfig()
