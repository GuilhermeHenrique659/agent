import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime
from autodev.config.models import AgentOverride, AgentsConfig, Effort
from autodev.domain.role import Role
from autodev.ports.agent_runtime import AgentRequest


class Answer(BaseModel):
    answer: int


@pytest.mark.live
def test_runs_trivial_request_against_real_model(tmp_path: Path) -> None:
    agents = AgentsConfig(
        defaults=AgentOverride(
            model="claude-sonnet-5-5",
            effort=Effort.LOW,
            max_turns=5,
            tools=[],
            max_budget_usd=0.5,
        )
    )
    request = AgentRequest(
        role=Role.IMPLEMENTER,
        profile=agents.profile_for(Role.IMPLEMENTER),
        instructions="Answer the question directly.",
        prompt="What is 2 + 3? Return it as `answer`.",
        workdir=tmp_path,
        output_model=Answer,
    )
    outcome = asyncio.run(ClaudeSdkRuntime().run(request))
    assert outcome.output.answer == 5
