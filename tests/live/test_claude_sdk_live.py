import asyncio
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from autodev.adapters.claude_sdk_runtime import ClaudeSdkRuntime
from autodev.config.models import AgentOverride, AgentsConfig, Effort, SettingSource
from autodev.domain.role import Role
from autodev.ports.agent_runtime import AgentRequest


class Answer(BaseModel):
    answer: int


def _setting_sources() -> list[SettingSource]:
    # e.g. AUTODEV_LIVE_SETTING_SOURCES=user,project when auth lives in ~/.claude
    raw = os.environ.get("AUTODEV_LIVE_SETTING_SOURCES", "project")
    sources: list[SettingSource] = []
    for s in raw.split(","):
        if s in ("user", "project", "local"):
            sources.append(s)  # type: ignore[arg-type]
    return sources


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
    runtime = ClaudeSdkRuntime(setting_sources=_setting_sources())
    outcome = asyncio.run(runtime.run(request))
    assert outcome.output.answer == 5
