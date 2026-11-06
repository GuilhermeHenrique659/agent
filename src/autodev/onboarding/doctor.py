from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from autodev.config.models import Effort, ProjectConfig
from autodev.domain.role import Role
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime

DOCTOR_HINTS = (
    "Check your network or proxy and that Claude Code is authenticated on this machine; "
    "check runtime.settings / runtime.setting_sources in .agent/config.yaml."
)
_BUDGET_USD = 0.2


class ConnectionAnswer(BaseModel):
    answer: int


@dataclass(frozen=True)
class DoctorResult:
    ok: bool
    detail: str
    cost_usd: float | None


async def check_connection(
    config: ProjectConfig, project_root: Path, runtime: AgentRuntime
) -> DoctorResult:
    """One cheap model call (implementer's model) to prove authentication and connectivity."""
    profile = config.agents.profile_for(Role.IMPLEMENTER).model_copy(
        update={
            "effort": Effort.LOW,
            "max_turns": 3,
            "tools": [],
            "skills": None,
            "max_budget_usd": _BUDGET_USD,
        }
    )
    request = AgentRequest(
        Role.IMPLEMENTER,
        profile,
        "Answer directly.",
        "What is 2 + 3? Return it as `answer`.",
        project_root,
        ConnectionAnswer,
    )
    try:
        outcome = await runtime.run(request)
    except AgentError as exc:
        return DoctorResult(False, str(exc), None)
    if outcome.output.answer != 5:
        return DoctorResult(False, f"Unexpected answer: {outcome.output.answer}", outcome.cost_usd)
    return DoctorResult(True, profile.model, outcome.cost_usd)


def describe(result: DoctorResult) -> str:
    cost = f", ${result.cost_usd:.4f}" if result.cost_usd is not None else ""
    return f"Connection OK ({result.detail}{cost})"
