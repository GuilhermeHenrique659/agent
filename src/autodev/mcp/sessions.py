import asyncio
import secrets
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from autodev.app.commands import CommandError
from autodev.mcp.bridge import BridgeIO, SessionCancelled
from autodev.ports.user_io import UserIO

CANCEL_NOTE = (
    "Cancellation requested. A running agent call cannot be interrupted; "
    "the session ends when it returns."
)


class SessionState(StrEnum):
    AWAITING_REPLY = "awaiting_reply"
    WORKING = "working"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({SessionState.FINISHED, SessionState.FAILED, SessionState.CANCELLED})


@dataclass(frozen=True)
class SessionEvent:
    session_id: str
    command: str
    state: SessionState
    messages: list[str]
    question: str | None
    error: str | None


class _Session:
    def __init__(self, session_id: str, command: str, path: Path) -> None:
        self.id = session_id
        self.command = command
        self.path = path
        self.bridge = BridgeIO()
        self.done = False
        self.cancelled = False
        self.error: str | None = None

    def run(self, flow: Callable[[UserIO], Awaitable[None]]) -> None:
        try:
            asyncio.run(self._call(flow))
        except SessionCancelled:
            pass
        except CommandError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.done = True
            self.bridge.notify_change()

    async def _call(self, flow: Callable[[UserIO], Awaitable[None]]) -> None:
        await flow(self.bridge)

    def event(self, extra: list[str] | None = None) -> SessionEvent:
        messages = [*self.bridge.drain(), *(extra or [])]
        question = None
        if self.done:
            if self.cancelled:
                state = SessionState.CANCELLED
            else:
                state = SessionState.FAILED if self.error else SessionState.FINISHED
        elif (question := self.bridge.pending_question) is not None:
            state = SessionState.AWAITING_REPLY
        else:
            state = SessionState.WORKING
        error = self.error if state == SessionState.FAILED else None
        return SessionEvent(self.id, self.command, state, messages, question, error)


class SessionManager:
    def __init__(self, wait_seconds: float = 45.0) -> None:
        self._wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def start(
        self, command: str, path: Path, flow: Callable[[UserIO], Awaitable[None]]
    ) -> SessionEvent:
        resolved = path.resolve()
        with self._lock:
            for other in self._sessions.values():
                if other.path == resolved and not other.done:
                    raise ValueError(
                        f"Session {other.id} ({other.command}) is still active for {resolved}; "
                        "finish it, or cancel it, first."
                    )
            session = _Session(secrets.token_hex(4), command, resolved)
            self._sessions[session.id] = session
        threading.Thread(target=session.run, args=(flow,), daemon=True).start()
        return self._wait(session)

    def reply(self, session_id: str, answer: str) -> SessionEvent:
        session = self._get(session_id)
        if session.done or session.bridge.pending_question is None:
            raise ValueError(f"Session {session_id} has no pending question to answer.")
        session.bridge.answer(answer)
        return self._wait(session)

    def poll(self, session_id: str) -> SessionEvent:
        event = self._wait(self._get(session_id))
        if event.state in TERMINAL:
            with self._lock:
                self._sessions.pop(session_id, None)
        return event

    def cancel(self, session_id: str) -> SessionEvent:
        session = self._get(session_id)
        session.cancelled = True
        session.bridge.cancel()
        return self._wait(session, [CANCEL_NOTE], until_done=True)

    def _get(self, session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown session: {session_id}")
        return session

    def _wait(
        self, session: _Session, extra: list[str] | None = None, until_done: bool = False
    ) -> SessionEvent:
        session.bridge.wait_for_change(
            lambda: (
                session.done or (not until_done and session.bridge.pending_question is not None)
            ),
            self._wait_seconds,
        )
        return session.event(extra)
