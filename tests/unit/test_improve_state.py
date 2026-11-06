import json
from pathlib import Path

from autodev.domain.story import Story, StoryStatus
from autodev.improve.state import mark_improved, should_suggest
from autodev.pipeline.state_store import StoryStore


def save(store: StoryStore, story_id: str, status: StoryStatus) -> None:
    store.save(Story(story_id, f"Story {story_id}", status=status))


def test_should_suggest_counts_new_done_stories(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    save(store, "S-0001", StoryStatus.DONE)
    save(store, "S-0002", StoryStatus.DONE)
    save(store, "S-0003", StoryStatus.IN_PROGRESS)
    assert should_suggest(tmp_path, store, 2)
    assert not should_suggest(tmp_path, store, 3)


def test_mark_improved_resets_count(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    save(store, "S-0001", StoryStatus.DONE)
    save(store, "S-0002", StoryStatus.DONE)
    mark_improved(tmp_path, store)
    state = json.loads((tmp_path / ".agent" / "runs" / "improve-state.json").read_text())
    assert state == {"done_story_ids": ["S-0001", "S-0002"]}
    assert not should_suggest(tmp_path, store, 1)
    save(store, "S-0003", StoryStatus.DONE)
    assert should_suggest(tmp_path, store, 1)
    assert not should_suggest(tmp_path, store, 2)


def test_missing_state_file_means_none_accounted(tmp_path: Path) -> None:
    store = StoryStore(tmp_path)
    save(store, "S-0001", StoryStatus.DONE)
    assert not (tmp_path / ".agent" / "runs" / "improve-state.json").exists()
    assert should_suggest(tmp_path, store, 1)
