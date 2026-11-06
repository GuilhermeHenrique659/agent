import asyncio
from pathlib import Path

import pytest

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.pipeline.architecture import review_architecture
from autodev.pipeline.outputs import AdrDraft, ArchitectureReview, DiscoveryResult
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error

RESULT = DiscoveryResult(
    title="Checkout",
    problem="Customers cannot pay",
    needs_architecture_review=True,
    architecture_notes="needs a payment gateway",
)


class Context:
    def paths(self) -> list[Path]:
        return [Path(".agent/domain/glossary.md")]


def proposal(title: str = "Use a payment gateway", notes: str = "Keep it behind a port") -> Step:
    return Step(
        ArchitectureReview(
            summary="Add gateway",
            adrs=[AdrDraft(title=title, context="c", decision="Stripe", consequences="fees")],
            components=["PaymentGateway", "OrderService"],
            design_notes=notes,
        )
    )


def run(root: Path, runtime: FakeAgentRuntime, io: FakeIO) -> str:
    return asyncio.run(
        review_architecture(
            Story("S-0001", "Pay", description="I want to pay"),
            RESULT,
            config=ProjectConfig(),
            project_root=root,
            runtime=runtime,
            context=Context(),
            io=io,
            log=RunLog(root, "S-0001"),
        )
    )


def test_accept_writes_adrs_and_returns_section(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("a")
    runtime.script(Role.ARCHITECT, proposal())
    section = run(tmp_path, runtime, io)
    adr = tmp_path / ".agent/architecture/adr/0001-use-a-payment-gateway.md"
    assert "Stripe" in adr.read_text()
    assert section == (
        "## Architecture decisions\n"
        "- .agent/architecture/adr/0001-use-a-payment-gateway.md: Use a payment gateway\n\n"
        "### Components\n- PaymentGateway\n- OrderService\n\n"
        "### Design notes\nKeep it behind a port\n"
    )
    assert io.prompts == ["[a]ccept, [f]eedback"]
    assert any("Use a payment gateway" in text and "PaymentGateway" in text for text in io.shown)
    prompt = runtime.requests_for(Role.ARCHITECT)[0].prompt
    assert "needs a payment gateway" in prompt


def test_empty_parts_are_omitted(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    runtime.script(
        Role.ARCHITECT, Step(ArchitectureReview(summary="s", components=["OnlyComponent"]))
    )
    section = run(tmp_path, runtime, FakeIO("a"))
    assert section == "## Architecture decisions\n\n### Components\n- OnlyComponent\n"


def test_feedback_reruns_with_previous_proposal(tmp_path: Path) -> None:
    runtime, io = FakeAgentRuntime(), FakeIO("x", "f", "use Adyen", "a")
    runtime.script(Role.ARCHITECT, proposal("Use Stripe"), proposal("Use Adyen"))
    section = run(tmp_path, runtime, io)
    second = runtime.requests_for(Role.ARCHITECT)[1].prompt
    assert "Use Stripe" in second and "use Adyen" in second
    assert "Use Adyen" in section and "Use Stripe" not in section
    assert [p.name for p in (tmp_path / ".agent/architecture/adr").glob("*.md")] == [
        "0001-use-adyen.md"
    ]


def test_agent_error_becomes_run_error(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    runtime.script(Role.ARCHITECT, Step(agent_error("boom")))
    with pytest.raises(RunError, match="boom"):
        run(tmp_path, runtime, FakeIO())
