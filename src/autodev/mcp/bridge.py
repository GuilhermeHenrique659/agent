import threading
from collections.abc import Callable


class SessionCancelled(Exception):
    """Raised inside a flow when its session was cancelled while (or before) it asked."""


class BridgeIO:
    """UserIO for a flow running in a background thread, driven by another thread."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._outbox: list[str] = []
        self._question: str | None = None
        self._answer: str | None = None
        self._cancelled = False

    @property
    def pending_question(self) -> str | None:
        with self._cond:
            return self._question

    def show(self, text: str) -> None:
        with self._cond:
            self._outbox.append(text)
            self._cond.notify_all()

    def ask(self, prompt: str) -> str:
        with self._cond:
            if self._cancelled:
                raise SessionCancelled
            self._question = prompt
            self._answer = None
            self._cond.notify_all()
            self._cond.wait_for(lambda: self._answer is not None or self._cancelled)
            if self._answer is None:
                self._question = None
                raise SessionCancelled
            answer, self._answer = self._answer, None
            return answer

    def answer(self, text: str) -> None:
        with self._cond:
            self._answer = text
            self._question = None
            self._cond.notify_all()

    def cancel(self) -> None:
        with self._cond:
            self._cancelled = True
            self._cond.notify_all()

    def drain(self) -> list[str]:
        with self._cond:
            messages, self._outbox = self._outbox, []
            return messages

    def notify_change(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def wait_for_change(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Block until `predicate()` holds or `timeout` elapses; return the predicate's value."""
        with self._cond:
            return self._cond.wait_for(predicate, timeout)
