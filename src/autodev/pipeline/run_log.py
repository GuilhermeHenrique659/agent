import json
from datetime import UTC, datetime
from pathlib import Path


class RunLog:
    def __init__(self, project_root: Path, story_id: str) -> None:
        started = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        directory = project_root / ".agent" / "runs" / story_id
        self.path = directory / f"{started}.jsonl"

    def append(
        self,
        event: str,
        *,
        role: str | None = None,
        task: str | None = None,
        ok: bool | None = None,
        cost_usd: float | None = None,
        turns: int | None = None,
        detail: str | None = None,
    ) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            "role": role,
            "task": task,
            "ok": ok,
            "cost_usd": cost_usd,
            "turns": turns,
            "detail": detail,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
