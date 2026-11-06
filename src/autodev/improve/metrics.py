import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FAILED_LINE = re.compile(r"^(\w+): failed$", re.MULTILINE)
_GATE_EVENTS = ("gate", "tdd_red", "tdd_green")


@dataclass(frozen=True)
class RoleStats:
    calls: int
    failures: int
    cost_usd: float


@dataclass(frozen=True)
class ProcessStats:
    runs: int = 0
    stories_done: int = 0
    total_cost_usd: float = 0.0
    cost_by_story: dict[str, float] = field(default_factory=dict)
    roles: dict[str, RoleStats] = field(default_factory=dict)
    tdd_red_failures: int = 0
    tdd_green_failures: int = 0
    gate_failures: dict[str, int] = field(default_factory=dict)
    review_rejections: int = 0
    qa_rejections: int = 0
    blocked_reasons: list[str] = field(default_factory=list)
    merge_conflicts: int = 0


def load_records(project_root: Path) -> list[dict[str, Any]]:
    """Every record of every run log; the parent directory name is added as `story`."""
    records: list[dict[str, Any]] = []
    for path in sorted((project_root / ".agent" / "runs").glob("*/*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                record["story"] = path.parent.name
                records.append(record)
    return records


def compute_stats(records: list[dict[str, Any]]) -> ProcessStats:
    runs = stories_done = tdd_red = tdd_green = review = qa = conflicts = 0
    cost_by_story: dict[str, float] = {}
    roles: dict[str, tuple[int, int, float]] = {}
    gate_failures: dict[str, int] = {}
    blocked: list[str] = []
    for r in records:
        event = r.get("event")
        ok = r.get("ok")
        cost = r.get("cost_usd") or 0.0
        if cost:
            story = str(r.get("story", ""))
            cost_by_story[story] = cost_by_story.get(story, 0.0) + cost
        if event == "run_started":
            runs += 1
        elif event == "story_done":
            stories_done += 1
        elif event == "agent_run":
            calls, failures, spent = roles.get(str(r.get("role")), (0, 0, 0.0))
            roles[str(r.get("role"))] = (calls + 1, failures + (ok is False), spent + cost)
        elif event == "review" and ok is False:
            review += 1
        elif event == "qa" and ok is False:
            qa += 1
        elif event == "story_blocked":
            blocked.append(str(r.get("detail") or ""))
        elif event == "merge_conflict":
            conflicts += 1
        if event == "tdd_red" and ok is False:
            tdd_red += 1
        elif event == "tdd_green" and ok is False:
            tdd_green += 1
        if event in _GATE_EVENTS:
            for name in _FAILED_LINE.findall(str(r.get("detail") or "")):
                gate_failures[name] = gate_failures.get(name, 0) + 1
    return ProcessStats(
        runs=runs,
        stories_done=stories_done,
        total_cost_usd=sum(cost_by_story.values()),
        cost_by_story=cost_by_story,
        roles={k: RoleStats(*v) for k, v in roles.items()},
        tdd_red_failures=tdd_red,
        tdd_green_failures=tdd_green,
        gate_failures=gate_failures,
        review_rejections=review,
        qa_rejections=qa,
        blocked_reasons=blocked,
        merge_conflicts=conflicts,
    )


def render_stats(stats: ProcessStats) -> str:
    lines = [
        "## Totals",
        f"- runs: {stats.runs}",
        f"- stories done: {stats.stories_done}",
        f"- total cost: ${stats.total_cost_usd:.2f}",
        "",
        "## Cost by story",
        *(f"- {s}: ${c:.2f}" for s, c in sorted(stats.cost_by_story.items())),
        "",
        "## Agent runs by role",
        *(
            f"- {role}: {r.calls} calls, {r.failures} failures, ${r.cost_usd:.2f}"
            for role, r in sorted(stats.roles.items())
        ),
        "",
        "## Failures",
        f"- TDD red failures: {stats.tdd_red_failures}",
        f"- TDD green failures: {stats.tdd_green_failures}",
        f"- review rejections: {stats.review_rejections}",
        f"- QA rejections: {stats.qa_rejections}",
        f"- merge conflicts: {stats.merge_conflicts}",
        "- gate failures:",
        *(f"  - {name}: {n}" for name, n in sorted(stats.gate_failures.items())),
        "- blocked stories:",
        *(f"  - {reason}" for reason in stats.blocked_reasons),
    ]
    return "\n".join(lines) + "\n"
