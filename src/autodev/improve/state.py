import json
from pathlib import Path

from autodev.domain.story import StoryStatus
from autodev.pipeline.state_store import StoryStore


def _state_path(project_root: Path) -> Path:
    return project_root / ".agent" / "runs" / "improve-state.json"


def _done_ids(store: StoryStore) -> list[str]:
    return [s.id for s in store.values() if s.status == StoryStatus.DONE]


def _accounted(project_root: Path) -> set[str]:
    path = _state_path(project_root)
    if not path.is_file():
        return set()
    return set(json.loads(path.read_text()).get("done_story_ids", []))


def new_done_count(project_root: Path, store: StoryStore) -> int:
    accounted = _accounted(project_root)
    return sum(1 for i in _done_ids(store) if i not in accounted)


def should_suggest(project_root: Path, store: StoryStore, every_n: int) -> bool:
    return new_done_count(project_root, store) >= every_n


def mark_improved(project_root: Path, store: StoryStore) -> None:
    path = _state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"done_story_ids": _done_ids(store)}, indent=2) + "\n")
