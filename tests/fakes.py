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


class FakeConversation[T: BaseModel]:
    def __init__(self, runtime: "FakeAgentRuntime", request: AgentRequest[T]) -> None:
        self._runtime = runtime
        self.request = request
        self.messages: list[str] = []
        self.closed = False

    async def send(self, message: str) -> AgentOutcome[T]:
        self.messages.append(message)
        return self._runtime._pop(self.request)

    async def close(self) -> None:
        self.closed = True


class FakeIO:
    """UserIO with scripted answers; records what was shown and asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = deque(answers)
        self.shown: list[str] = []
        self.prompts: list[str] = []

    def show(self, text: str) -> None:
        self.shown.append(text)

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"No scripted answer left for prompt {prompt!r}")
        return self.answers.popleft()


class FakeAgentRuntime:
    def __init__(self) -> None:
        self._steps: dict[Role, deque[Step]] = defaultdict(deque)
        self.requests: list[AgentRequest[Any]] = []
        self.conversations: list[FakeConversation[Any]] = []

    def script(self, role: Role, *steps: Step) -> None:
        self._steps[role].extend(steps)

    def requests_for(self, role: Role) -> list[AgentRequest[Any]]:
        return [r for r in self.requests if r.role == role]

    def messages_for(self, role: Role) -> list[str]:
        return [m for c in self.conversations if c.request.role == role for m in c.messages]

    async def run[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]:
        self.requests.append(request)
        return self._pop(request)

    async def start_conversation[T: BaseModel](
        self, request: AgentRequest[T]
    ) -> FakeConversation[T]:
        self.requests.append(request)
        conversation = FakeConversation(self, request)
        self.conversations.append(conversation)
        return conversation

    def _pop[T: BaseModel](self, request: AgentRequest[T]) -> AgentOutcome[T]:
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
