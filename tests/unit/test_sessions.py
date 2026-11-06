import asyncio
import threading
from pathlib import Path

import pytest

from autodev.app.commands import CommandError
from autodev.mcp.sessions import SessionManager, SessionState
from autodev.ports.user_io import UserIO


async def ask_flow(io: UserIO) -> None:
    io.show("hello")
    name = io.ask("Name?")
    io.show(f"hi {name}")


def test_flow_question_reply_finish_sequence(tmp_path: Path) -> None:
    manager = SessionManager(wait_seconds=5)
    first = manager.start("demo", tmp_path, ask_flow)
    assert first.state == SessionState.AWAITING_REPLY
    assert first.question == "Name?"
    assert first.messages == ["hello"]
    assert first.command == "demo"
    last = manager.reply(first.session_id, "Ada")
    assert last.state == SessionState.FINISHED
    assert last.messages == ["hi Ada"]
    assert last.question is None
    assert last.error is None


def test_slow_flow_returns_working_then_poll_finishes(tmp_path: Path) -> None:
    release = threading.Event()

    async def slow(io: UserIO) -> None:
        io.show("started")
        await _wait(release)
        io.show("done")

    manager = SessionManager(wait_seconds=0.3)
    first = manager.start("slow", tmp_path, slow)
    assert first.state == SessionState.WORKING
    assert first.messages == ["started"]
    release.set()
    last = manager.poll(first.session_id)
    assert last.state == SessionState.FINISHED
    assert last.messages == ["done"]


async def _wait(event: threading.Event) -> None:
    await asyncio.to_thread(event.wait, 5)


def test_flow_error_becomes_failed(tmp_path: Path) -> None:
    async def failing(io: UserIO) -> None:
        raise CommandError("bad config")

    async def crashing(io: UserIO) -> None:
        raise RuntimeError("kaboom")

    manager = SessionManager(wait_seconds=5)
    failed = manager.start("a", tmp_path, failing)
    assert failed.state == SessionState.FAILED
    assert failed.error == "bad config"
    crashed = manager.start("b", tmp_path, crashing)
    assert crashed.state == SessionState.FAILED
    assert crashed.error == "RuntimeError: kaboom"


def test_one_active_session_per_path(tmp_path: Path) -> None:
    manager = SessionManager(wait_seconds=5)
    first = manager.start("demo", tmp_path, ask_flow)
    with pytest.raises(ValueError, match=first.session_id):
        manager.start("demo", tmp_path / ".", ask_flow)
    other = tmp_path / "other"
    other.mkdir()
    assert manager.start("demo", other, ask_flow).state == SessionState.AWAITING_REPLY


def test_reply_without_pending_question_raises(tmp_path: Path) -> None:
    release = threading.Event()

    async def slow(io: UserIO) -> None:
        await _wait(release)

    manager = SessionManager(wait_seconds=0.1)
    event = manager.start("slow", tmp_path, slow)
    assert event.state == SessionState.WORKING
    with pytest.raises(ValueError, match="no pending question"):
        manager.reply(event.session_id, "x")
    release.set()


def test_cancel_pending_session(tmp_path: Path) -> None:
    manager = SessionManager(wait_seconds=5)
    first = manager.start("demo", tmp_path, ask_flow)
    cancelled = manager.cancel(first.session_id)
    assert cancelled.state == SessionState.CANCELLED
    assert any("cannot be interrupted" in m for m in cancelled.messages)
    assert manager.start("demo", tmp_path, ask_flow).state == SessionState.AWAITING_REPLY


def test_unknown_session_raises() -> None:
    manager = SessionManager(wait_seconds=0.1)
    with pytest.raises(KeyError):
        manager.reply("nope", "x")
    with pytest.raises(KeyError):
        manager.poll("nope")
    with pytest.raises(KeyError):
        manager.cancel("nope")


def test_finished_session_removed_after_poll(tmp_path: Path) -> None:
    async def quick(io: UserIO) -> None:
        io.show("ok")

    manager = SessionManager(wait_seconds=5)
    event = manager.start("quick", tmp_path, quick)
    assert event.state == SessionState.FINISHED
    assert manager.poll(event.session_id).state == SessionState.FINISHED
    with pytest.raises(KeyError):
        manager.poll(event.session_id)
    assert manager.start("quick", tmp_path, quick).state == SessionState.FINISHED
