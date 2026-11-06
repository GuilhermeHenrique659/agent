from pathlib import Path

import pytest

from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.pipeline.state_store import StoryStore


def test_save_then_get_round_trips_story(tmp_path: Path) -> None:
    story = Story(
        id="S-0001",
        title="Title: with colon",
        description="Line one\n\nLine two with --- inside\n",
        status=StoryStatus.BLOCKED,
        acceptance_criteria=["a", "b: c"],
        tasks=[Task("T-1", "one", TaskStatus.DONE), Task("T-2", "two")],
        attempts=2,
        blocked_from=StoryStatus.IN_REVIEW,
        blocked_reason="because",
    )
    store = StoryStore(tmp_path)
    store.save(story)
    assert (tmp_path / ".agent" / "backlog" / "S-0001.md").exists()
    assert StoryStore(tmp_path).get("S-0001") == story


def test_round_trip_minimal_story(tmp_path: Path) -> None:
    story = Story("S-0001", "t")
    store = StoryStore(tmp_path)
    store.save(story)
    assert store.get("S-0001") == story


def test_get_missing_raises_key_error(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        StoryStore(tmp_path).get("S-0001")


def test_has(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    assert not store.has("S-0001")
    store.save(Story("S-0001", "t"))
    assert store.has("S-0001")


def test_values_sorted_by_id(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    store.save(Story("S-0002", "b"))
    store.save(Story("S-0001", "a"))
    assert [s.id for s in store.values()] == ["S-0001", "S-0002"]


def test_next_id_starts_at_one_and_increments(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    assert store.next_id() == "S-0001"
    store.save(Story("S-0001", "a"))
    store.save(Story("S-0005", "b"))
    assert store.next_id() == "S-0006"
