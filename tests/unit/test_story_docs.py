from autodev.pipeline.outputs import DiscoveryResult, EventStorming
from autodev.pipeline.story_docs import render_discovery, render_refined


def test_render_discovery_sections() -> None:
    result = DiscoveryResult(
        title="T",
        problem="The problem",
        event_storming=EventStorming(domain_events=["OrderPlaced"], actors=["Customer"]),
        needs_architecture_review=True,
        architecture_notes="New queue",
        open_questions=["Which currency?"],
    )
    text = render_discovery("raw request", result)
    assert "## Request\nraw request" in text
    assert "## Problem\nThe problem" in text
    assert "## Event storming" in text
    assert "- OrderPlaced" in text and "- Customer" in text
    assert "Commands" not in text
    assert "## Architecture\nNeeds architecture review.\nNew queue" in text
    assert "## Open questions\n- Which currency?" in text
    plain = render_discovery("r", DiscoveryResult(title="T", problem="p"))
    assert "## Architecture" not in plain
    assert "## Open questions" not in plain
    assert "## Event storming" not in plain


def test_render_refined_replaces_previous_user_story() -> None:
    discovery = "## Request\nraw\n\n## Problem\np\n"
    first = render_refined("As a user, I want x", discovery)
    assert first == "## User story\nAs a user, I want x\n\n## Request\nraw\n\n## Problem\np\n"
    second = render_refined("As a user, I want y", first)
    assert second.count("## User story") == 1
    assert "I want y" in second and "I want x" not in second
    assert "## Request\nraw" in second and "## Problem\np" in second
