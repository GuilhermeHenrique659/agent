import re
from pathlib import Path
from typing import Any

import yaml

from autodev.domain.story import Story, StoryStatus, Task, TaskStatus

_ID_RE = re.compile(r"^S-(\d+)$")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.DOTALL)


class StoryStore:
    def __init__(self, project_root: Path) -> None:
        self._dir = project_root / ".agent" / "backlog"

    def save(self, story: Story) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
            "id": story.id,
            "title": story.title,
            "status": story.status.value,
            "attempts": story.attempts,
            "blocked_from": story.blocked_from.value if story.blocked_from else None,
            "blocked_reason": story.blocked_reason,
            "acceptance_criteria": story.acceptance_criteria,
            "tasks": [
                {
                    "id": t.id,
                    "title": t.title,
                    "status": t.status.value,
                    "coverage": {c: list(names) for c, names in t.coverage.items()},
                }
                for t in story.tasks
            ],
        }
        front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
        self._path(story.id).write_text(f"---\n{front}---\n{story.description}")

    def get(self, story_id: str) -> Story:
        path = self._path(story_id)
        if not path.is_file():
            raise KeyError(story_id)
        return self._parse(path.read_text())

    def has(self, story_id: str) -> bool:
        return self._path(story_id).is_file()

    def values(self) -> list[Story]:
        if not self._dir.is_dir():
            return []
        stories = [self._parse(p.read_text()) for p in self._dir.glob("*.md")]
        return sorted(stories, key=lambda s: s.id)

    def next_id(self) -> str:
        numbers = [
            int(m.group(1))
            for p in (self._dir.glob("*.md") if self._dir.is_dir() else [])
            if (m := _ID_RE.match(p.stem))
        ]
        return f"S-{max(numbers, default=0) + 1:04d}"

    def _path(self, story_id: str) -> Path:
        return self._dir / f"{story_id}.md"

    @staticmethod
    def _parse(text: str) -> Story:
        match = _FRONTMATTER_RE.match(text)
        if match is None:
            raise ValueError("Story file is missing YAML frontmatter")
        meta = yaml.safe_load(match.group(1))
        blocked_from = meta["blocked_from"]
        return Story(
            id=meta["id"],
            title=meta["title"],
            description=match.group(2),
            status=StoryStatus(meta["status"]),
            acceptance_criteria=list(meta["acceptance_criteria"]),
            tasks=[
                Task(
                    t["id"],
                    t["title"],
                    TaskStatus(t["status"]),
                    {int(c): list(names) for c, names in (t.get("coverage") or {}).items()},
                )
                for t in meta["tasks"]
            ],
            attempts=meta["attempts"],
            blocked_from=StoryStatus(blocked_from) if blocked_from else None,
            blocked_reason=meta["blocked_reason"],
        )
