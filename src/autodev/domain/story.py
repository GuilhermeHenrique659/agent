from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise


class StoryStatus(StrEnum):
    DRAFT = "draft"
    DISCOVERED = "discovered"
    REFINED = "refined"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    IN_QA = "in_qa"
    DONE = "done"
    BLOCKED = "blocked"


class TaskStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


class InvalidTransition(Exception):
    pass


_CHAIN = [
    StoryStatus.DRAFT,
    StoryStatus.DISCOVERED,
    StoryStatus.REFINED,
    StoryStatus.APPROVED,
    StoryStatus.IN_PROGRESS,
    StoryStatus.IN_REVIEW,
    StoryStatus.IN_QA,
    StoryStatus.DONE,
]
_NEXT = dict(pairwise(_CHAIN))
_REVIEW_STATES = (StoryStatus.IN_REVIEW, StoryStatus.IN_QA)


@dataclass
class Task:
    id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING

    def complete(self) -> None:
        if self.status == TaskStatus.DONE:
            raise InvalidTransition(f"Task {self.id} is already done")
        self.status = TaskStatus.DONE


@dataclass
class Story:
    id: str
    title: str
    description: str = ""
    status: StoryStatus = StoryStatus.DRAFT
    acceptance_criteria: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    attempts: int = 0
    blocked_from: StoryStatus | None = None
    blocked_reason: str | None = None

    def advance(self, to: StoryStatus) -> None:
        if _NEXT.get(self.status) != to:
            raise InvalidTransition(f"Cannot advance {self.status} -> {to}")
        if to == StoryStatus.APPROVED and not (self.tasks and self.acceptance_criteria):
            raise InvalidTransition("Approval requires tasks and acceptance criteria")
        if to == StoryStatus.IN_REVIEW and any(t.status != TaskStatus.DONE for t in self.tasks):
            raise InvalidTransition("Review requires every task to be done")
        self.status = to

    def record_discovery(self, title: str, description: str) -> None:
        if self.status != StoryStatus.DRAFT:
            raise InvalidTransition(f"Cannot record discovery from {self.status}")
        self.title = title
        self.description = description
        self.status = StoryStatus.DISCOVERED

    def refine(
        self,
        title: str,
        description: str,
        acceptance_criteria: list[str],
        task_titles: list[str],
    ) -> None:
        if self.status not in (StoryStatus.DISCOVERED, StoryStatus.REFINED):
            raise InvalidTransition(f"Cannot refine from {self.status}")
        if not (acceptance_criteria and task_titles):
            raise InvalidTransition("Refinement requires acceptance criteria and tasks")
        self.title = title
        self.description = description
        self.acceptance_criteria = list(acceptance_criteria)
        self.tasks = [Task(f"T{i}", t) for i, t in enumerate(task_titles, start=1)]
        self.status = StoryStatus.REFINED

    def send_back(self, reason: str, max_attempts: int) -> None:
        if self.status not in _REVIEW_STATES:
            raise InvalidTransition(f"Cannot send back from {self.status}")
        origin = self.status
        self.attempts += 1
        if self.attempts >= max_attempts:
            self._block_from(origin, reason)
        else:
            self.status = StoryStatus.IN_PROGRESS

    def block(self, reason: str) -> None:
        if self.status in (StoryStatus.DONE, StoryStatus.BLOCKED):
            raise InvalidTransition(f"Cannot block from {self.status}")
        self._block_from(self.status, reason)

    def resume(self) -> None:
        if self.status != StoryStatus.BLOCKED or self.blocked_from is None:
            raise InvalidTransition("Only a blocked story can be resumed")
        origin = self.blocked_from
        self.status = StoryStatus.IN_PROGRESS if origin in _REVIEW_STATES else origin
        self.blocked_from = None
        self.blocked_reason = None
        self.attempts = 0

    def _block_from(self, origin: StoryStatus, reason: str) -> None:
        self.status = StoryStatus.BLOCKED
        self.blocked_from = origin
        self.blocked_reason = reason
