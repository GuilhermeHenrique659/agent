import json
from pathlib import Path
from typing import Any

from autodev.improve.metrics import (
    ProcessStats,
    RoleStats,
    compute_stats,
    load_records,
    render_stats,
)


def rec(event: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event": event,
        "role": None,
        "task": None,
        "ok": None,
        "cost_usd": None,
        "turns": None,
        "detail": None,
    }
    return {**base, **kw}


def write_log(root: Path, story: str, name: str, lines: list[str]) -> None:
    directory = root / ".agent" / "runs" / story
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("\n".join(lines) + "\n")


def test_load_records_reads_all_runs_and_skips_malformed_lines(tmp_path: Path) -> None:
    write_log(
        tmp_path,
        "S-0001",
        "a.jsonl",
        [json.dumps(rec("run_started")), "not json {", "", json.dumps(rec("story_done"))],
    )
    write_log(tmp_path, "S-0002", "b.jsonl", [json.dumps(rec("run_started"))])
    (tmp_path / ".agent" / "runs" / "improve-state.json").write_text("{}")
    records = load_records(tmp_path)
    assert len(records) == 3
    assert sorted(r["story"] for r in records) == ["S-0001", "S-0001", "S-0002"]
    assert load_records(tmp_path / "nowhere") == []


def sample_records() -> list[dict[str, Any]]:
    return [
        rec("run_started", story="S-0001", detail="approved"),
        rec("agent_run", story="S-0001", role="test_writer", ok=True, cost_usd=0.5),
        rec("agent_run", story="S-0001", role="implementer", ok=False, detail="boom"),
        rec("agent_run", story="S-0001", role="implementer", ok=True, cost_usd=1.25),
        rec("tdd_red", story="S-0001", ok=False, detail="tdd_red: tests passed"),
        rec("tdd_green", story="S-0001", ok=False, detail="tdd_green: still red"),
        rec("tdd_green", story="S-0001", ok=True, detail="tdd_green: ok"),
        rec("gate", story="S-0001", ok=False, detail="build: ok\ntest: failed\nboom\nlint: failed"),
        rec("gate", story="S-0001", ok=False, detail="test: failed\nx"),
        rec("gate", story="S-0001", ok=True, detail="build: ok\ntest: ok"),
        rec("review", story="S-0001", ok=False, detail="no"),
        rec("review", story="S-0001", ok=True, detail="yes"),
        rec("qa", story="S-0001", ok=False, detail="no"),
        rec("merge_conflict", story="S-0001", detail="a.py"),
        rec("story_done", story="S-0001"),
        rec("run_started", story="S-0002", detail="approved"),
        rec("agent_run", story="S-0002", role="qa", ok=True, cost_usd=0.75),
        rec("story_blocked", story="S-0002", detail="red phase failed"),
    ]


def test_compute_stats_counts() -> None:
    stats = compute_stats(sample_records())
    assert stats.runs == 2
    assert stats.stories_done == 1
    assert stats.total_cost_usd == 2.5
    assert stats.cost_by_story == {"S-0001": 1.75, "S-0002": 0.75}
    assert stats.roles == {
        "test_writer": RoleStats(1, 0, 0.5),
        "implementer": RoleStats(2, 1, 1.25),
        "qa": RoleStats(1, 0, 0.75),
    }
    assert stats.tdd_red_failures == 1
    assert stats.tdd_green_failures == 1
    assert stats.gate_failures == {"test": 2, "lint": 1}
    assert stats.review_rejections == 1
    assert stats.qa_rejections == 1
    assert stats.blocked_reasons == ["red phase failed"]
    assert stats.merge_conflicts == 1


def test_render_stats_sections() -> None:
    text = render_stats(compute_stats(sample_records()))
    for heading in ("## Totals", "## Cost by story", "## Agent runs by role", "## Failures"):
        assert heading in text
    assert "$1.75" in text and "$2.50" in text
    assert "implementer" in text
    assert "red phase failed" in text
    assert "test: 2" in text
    empty = render_stats(ProcessStats())
    assert "## Totals" in empty
