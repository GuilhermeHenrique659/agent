import asyncio
from pathlib import Path

import pytest

from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.pipeline.outputs import (
    DiscoveryResult,
    DiscoveryTurn,
    EventStorming,
    GlossaryEntry,
    RefinedTask,
    RefinementOutput,
)
from autodev.pipeline.request_flow import handle_request
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error

HEADER = "| Term | Definition | Notes |\n|------|------------|-------|\n"


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".agent" / "domain").mkdir(parents=True)
    (root / ".agent" / "domain" / "glossary.md").write_text(f"# Glossary\n\n{HEADER}")
    return root


def request(root: Path, runtime: FakeAgentRuntime, text: str) -> Story:
    return asyncio.run(
        handle_request(
            text,
            config=ProjectConfig(),
            project_root=root,
            runtime=runtime,
            store=StoryStore(root),
            context=KnowledgeFolder(root),
            io=FakeIO(),
            log_factory=lambda sid: RunLog(root, sid),
        )
    )


def test_request_reaches_refined_and_updates_domain_docs(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    result = DiscoveryResult(
        title="Checkout",
        problem="Customers cannot pay",
        glossary=[GlossaryEntry(term="Cart", definition="Items to buy")],
        event_storming=EventStorming(domain_events=["OrderPlaced"]),
    )
    runtime = FakeAgentRuntime()
    runtime.script(Role.DISCOVERY, Step(DiscoveryTurn(message="ok", done=True, result=result)))
    runtime.script(
        Role.REFINER,
        Step(
            RefinementOutput(
                title="Checkout flow",
                user_story="As a customer, I want to pay",
                acceptance_criteria=["Given a cart When I pay Then an order exists"],
                tasks=[RefinedTask(title="Place order")],
            )
        ),
    )
    story = request(root, runtime, "x" * 80 + "\nsecond line")
    assert story.id == "S-0001"
    assert story.status == StoryStatus.REFINED
    saved = StoryStore(root).get("S-0001")
    assert saved.status == StoryStatus.REFINED
    assert saved.title == "Checkout flow"
    assert saved.description.startswith("## User story\nAs a customer, I want to pay")
    assert "## Problem\nCustomers cannot pay" in saved.description
    assert [t.id for t in saved.tasks] == ["T1"]
    assert "| Cart | Items to buy |  |" in (root / ".agent/domain/glossary.md").read_text()
    events = (root / ".agent/domain/events.md").read_text()
    assert "## S-0001: Checkout" in events and "- OrderPlaced" in events


def test_discovery_failure_leaves_story_saved_as_draft(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    runtime = FakeAgentRuntime()
    runtime.script(Role.DISCOVERY, Step(agent_error("boom")))
    with pytest.raises(RunError, match="boom"):
        request(root, runtime, "Pay with card")
    saved = StoryStore(root).get("S-0001")
    assert saved.status == StoryStatus.DRAFT
    assert saved.title == "Pay with card"
    assert saved.description == "Pay with card"
