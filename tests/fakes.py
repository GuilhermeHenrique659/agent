from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from autodev.domain.role import Role
from autodev.ports.agent_runtime import AgentError, AgentOutcome, AgentRequest


@dataclass
class Step:
    """One scripted agent run: optionally edit files, then return `result` or raise it."""

    result: BaseModel | Exception
    edit: Callable[[Path], None] | None = None
    cost_usd: float | None = 0.01
    turns: int = 1


class FakeAgentRuntime:
    def __init__(self) -> None:
        self._steps: dict[Role, deque[Step]] = defaultdict(deque)
        self.requests: list[AgentRequest[Any]] = []

    def script(self, role: Role, *steps: Step) -> None:
        self._steps[role].extend(steps)

    def requests_for(self, role: Role) -> list[AgentRequest[Any]]:
        return [r for r in self.requests if r.role == role]

    async def run[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]:
        self.requests.append(request)
        queue = self._steps[request.role]
        if not queue:
            raise AssertionError(f"No scripted step left for role {request.role.value}")
        step = queue.popleft()
        if step.edit is not None:
            step.edit(request.workdir)
        if isinstance(step.result, Exception):
            raise step.result
        output = request.output_model.model_validate(step.result.model_dump())
        return AgentOutcome(output=output, cost_usd=step.cost_usd, turns=step.turns)


def agent_error(message: str = "boom") -> AgentError:
    return AgentError(message)
