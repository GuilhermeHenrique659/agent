from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from autodev.config.models import AgentProfile
from autodev.domain.role import Role


@dataclass(frozen=True)
class AgentRequest[T: BaseModel]:
    role: Role
    profile: AgentProfile
    instructions: str  # role system prompt, appended to the Claude Code preset
    prompt: str  # the task prompt
    workdir: Path
    output_model: type[T]


@dataclass(frozen=True)
class AgentOutcome[T: BaseModel]:
    output: T
    cost_usd: float | None
    turns: int


class AgentError(Exception):
    pass


class AgentRuntime(Protocol):
    async def run[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]: ...
