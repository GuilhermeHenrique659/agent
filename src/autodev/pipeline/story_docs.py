import re

from autodev.domain.story import Story
from autodev.pipeline.outputs import DiscoveryResult

_USER_STORY_RE = re.compile(r"\A## User story\n.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE)


def render_discovery(request: str, result: DiscoveryResult) -> str:
    sections = [f"## Request\n{request.strip()}", f"## Problem\n{result.problem.strip()}"]
    es = result.event_storming
    groups = [
        ("Domain events", es.domain_events),
        ("Commands", es.commands),
        ("Actors", es.actors),
        ("Aggregates", es.aggregates),
        ("Policies", es.policies),
        ("Read models", es.read_models),
    ]
    blocks = [
        f"### {name}\n" + "\n".join(f"- {i}" for i in items) for name, items in groups if items
    ]
    if blocks:
        sections.append("## Event storming\n" + "\n\n".join(blocks))
    if result.needs_architecture_review:
        notes = result.architecture_notes.strip()
        sections.append(
            "## Architecture\nNeeds architecture review." + (f"\n{notes}" if notes else "")
        )
    if result.open_questions:
        sections.append("## Open questions\n" + "\n".join(f"- {q}" for q in result.open_questions))
    return "\n\n".join(sections) + "\n"


def render_refined(user_story: str, discovery_description: str) -> str:
    rest = _USER_STORY_RE.sub("", discovery_description.lstrip("\n"), count=1).lstrip("\n")
    return f"## User story\n{user_story.strip()}\n\n{rest}"


def render_story_summary(story: Story) -> str:
    match = _USER_STORY_RE.match(story.description.lstrip("\n"))
    user_story = match.group(0).removeprefix("## User story").strip() if match else ""
    parts = [f"# {story.id}: {story.title}"]
    if user_story:
        parts.append(f"## User story\n{user_story}")
    if story.acceptance_criteria:
        parts.append(
            "## Acceptance criteria\n" + "\n".join(f"- {c}" for c in story.acceptance_criteria)
        )
    if story.tasks:
        parts.append("## Tasks\n" + "\n".join(f"- {t.id}: {t.title}" for t in story.tasks))
    return "\n\n".join(parts) + "\n"
