import threading

import pytest

from autodev.mcp.bridge import BridgeIO, SessionCancelled


def test_show_is_buffered_and_drained() -> None:
    bridge = BridgeIO()
    bridge.show("one")
    bridge.show("two")
    assert bridge.drain() == ["one", "two"]
    assert bridge.drain() == []


def test_ask_blocks_until_answer() -> None:
    bridge = BridgeIO()
    answers: list[str] = []
    thread = threading.Thread(target=lambda: answers.append(bridge.ask("Name?")))
    thread.start()
    assert bridge.wait_for_change(lambda: bridge.pending_question is not None, 5)
    assert bridge.pending_question == "Name?"
    assert answers == []
    bridge.answer("Ada")
    thread.join(5)
    assert answers == ["Ada"]
    assert bridge.pending_question is None


def test_cancel_raises_in_pending_ask() -> None:
    bridge = BridgeIO()
    errors: list[BaseException] = []

    def target() -> None:
        try:
            bridge.ask("Name?")
        except SessionCancelled as exc:
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    assert bridge.wait_for_change(lambda: bridge.pending_question is not None, 5)
    bridge.cancel()
    thread.join(5)
    assert len(errors) == 1
    with pytest.raises(SessionCancelled):
        bridge.ask("again?")
