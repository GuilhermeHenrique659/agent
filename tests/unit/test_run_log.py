import json
from pathlib import Path

from autodev.pipeline.run_log import RunLog


def test_appends_json_lines_per_event(tmp_path: Path) -> None:
    log = RunLog(tmp_path, "S-0001")
    log.append("run_started")
    log.append("agent_run", role="implementer", task="T1", ok=True, cost_usd=0.5, turns=3)
    files = list((tmp_path / ".agent" / "runs" / "S-0001").glob("*.jsonl"))
    assert files == [log.path]
    assert log.path.stem.endswith("Z") and len(log.path.stem) == 16
    first, second = (json.loads(line) for line in log.path.read_text().splitlines())
    keys = {"ts", "event", "role", "task", "ok", "cost_usd", "turns", "detail"}
    assert set(first) == keys == set(second)
    assert first["event"] == "run_started" and first["role"] is None and first["ok"] is None
    assert second["role"] == "implementer"
    assert second["task"] == "T1"
    assert (second["ok"], second["cost_usd"], second["turns"]) == (True, 0.5, 3)
