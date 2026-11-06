import asyncio
from pathlib import Path

from autodev.config.models import Effort, ProjectConfig
from autodev.domain.role import Role
from autodev.onboarding.doctor import ConnectionAnswer, check_connection
from tests.fakes import FakeAgentRuntime, Step, agent_error


def config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "agents": {
                "defaults": {"model": "big", "effort": "high", "max_budget_usd": 9},
                "roles": {"implementer": {"model": "small", "tools": ["Bash"], "skills": "all"}},
            }
        }
    )


def test_ok_when_answer_is_five_uses_cheap_profile(tmp_path: Path) -> None:
    fake = FakeAgentRuntime()
    fake.script(Role.IMPLEMENTER, Step(ConnectionAnswer(answer=5), cost_usd=0.02))
    result = asyncio.run(check_connection(config(), tmp_path, fake))
    assert result.ok
    assert result.cost_usd == 0.02
    assert "small" in result.detail
    (request,) = fake.requests_for(Role.IMPLEMENTER)
    profile = request.profile
    assert profile.model == "small"
    assert profile.effort == Effort.LOW
    assert profile.tools == []
    assert profile.skills is None
    assert profile.max_turns == 3
    assert profile.max_budget_usd == 0.2
    assert request.workdir == tmp_path
    assert request.output_model is ConnectionAnswer


def test_failure_on_agent_error_or_wrong_answer(tmp_path: Path) -> None:
    fake = FakeAgentRuntime()
    fake.script(
        Role.IMPLEMENTER,
        Step(agent_error("auth failed")),
        Step(ConnectionAnswer(answer=6)),
    )
    failed = asyncio.run(check_connection(config(), tmp_path, fake))
    assert not failed.ok
    assert "auth failed" in failed.detail
    wrong = asyncio.run(check_connection(config(), tmp_path, fake))
    assert not wrong.ok
    assert "6" in wrong.detail
