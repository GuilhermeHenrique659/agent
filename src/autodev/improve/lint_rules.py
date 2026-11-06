from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story
from autodev.improve.selection import ask_selection
from autodev.pipeline import prompts
from autodev.pipeline.outputs import LintProposal, LintRule
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentError, AgentRequest, AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_TITLE = "Enforce standards with lint rules"
_PROMPT = "Create a story for which rules? (e.g. 1,3 / all / none)"

_INTRO = "Turn project standards into lint rules that the quality gates enforce automatically."

_HOW_TO_TEST = (
    "Prove each rule test-first. Put a fixture file containing a violation of the rule "
    "under tests/ (so the TDD guard treats it as a test file) and exclude those fixtures "
    "from the regular lint command, so the quality gates stay green. The test runs the "
    "linter directly on the fixture and expects the violation to be reported: it fails "
    "until the rule is enabled (red). Then enable the rule in the lint configuration (green) "
    "and make the regular lint command pass on the real code. "
    "Existing violations in the codebase must be fixed. "
    "Update `commands.lint` in `.agent/config.yaml` only if a new tool is introduced."
)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _render_rule(n: int, rule: LintRule) -> str:
    return f"{n}. [{rule.tool}] {rule.rule} — enforces: {rule.standard}\n   {rule.rationale}\n"


def _description(selected: list[LintRule]) -> str:
    rows = "\n".join(
        f"| {_cell(r.tool)} | {_cell(r.rule)} | {_cell(r.standard)} | {_cell(r.rationale)} |"
        for r in selected
    )
    table = f"| Tool | Rule | Enforces | Rationale |\n|---|---|---|---|\n{rows}"
    return (
        f"{_INTRO}\n\n## Rules\n\n{table}\n\n## How to test\n\n{_HOW_TO_TEST}\n\n"
        "Suggested by autodev improve --lint."
    )


async def run_lint_proposal(
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    context: ContextSource,
    store: StoryStore,
    io: UserIO,
    log: RunLog,
) -> Story | None:
    role = Role.IMPROVER
    profile = config.agents.profile_for(role)
    instructions = prompts.load_instructions(role, profile, project_root / ".agent")
    prompt = prompts.build_lint_prompt(context.paths(), config.commands.lint)
    request = AgentRequest(role, profile, instructions, prompt, project_root, LintProposal)
    try:
        outcome = await runtime.run(request)
    except AgentError as exc:
        log.append("improve_lint", role=role.value, ok=False, detail=str(exc))
        raise RunError(str(exc)) from exc
    proposal = outcome.output
    log.append(
        "improve_lint",
        role=role.value,
        ok=True,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        detail=proposal.summary,
    )
    io.show(proposal.summary)
    if not proposal.rules:
        return None
    for n, rule in enumerate(proposal.rules, start=1):
        io.show(_render_rule(n, rule))
    selection = ask_selection(io, _PROMPT, len(proposal.rules))
    if not selection:
        return None
    story = Story(
        store.next_id(), _TITLE, description=_description([proposal.rules[i] for i in selection])
    )
    store.save(story)
    log.append("improvement_created", detail=story.id)
    return story
