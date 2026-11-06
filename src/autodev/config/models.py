from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from autodev.domain.role import Role

_STRICT = ConfigDict(extra="forbid")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TURNS = 50


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class AgentProfile(BaseModel):
    model_config = _STRICT

    model: str = Field(min_length=1)
    effort: Effort
    max_turns: int = Field(ge=1)
    tools: list[str] | None
    prompt: str | None


class AgentOverride(BaseModel):
    model_config = _STRICT

    model: str | None = Field(default=None, min_length=1)
    effort: Effort | None = None
    max_turns: int | None = Field(default=None, ge=1)
    tools: list[str] | None = None
    prompt: str | None = None


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
            tools=specific.tools if specific.tools is not None else d.tools,
            prompt=specific.prompt if specific.prompt is not None else d.prompt,
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


class ProjectConfig(BaseModel):
    model_config = _STRICT

    commands: CommandsConfig = CommandsConfig()
    git: GitConfig = GitConfig()
    limits: LimitsConfig = LimitsConfig()
    tdd: TddConfig = TddConfig()
    agents: AgentsConfig = AgentsConfig()
