from importlib.resources import files
from pathlib import Path

from autodev.config.loader import ConfigError
from autodev.config.models import AgentProfile
from autodev.domain.role import Role
from autodev.domain.story import Story, Task
from autodev.gates.runner import GateReport
from autodev.pipeline.outputs import (
    ArchitectureReview,
    DiscoveryResult,
    Finding,
    QaOutput,
    TechBrief,
)

MAX_FEEDBACK_CHARS = 8000
PRECEDENCE = (
    "## Precedence\n"
    "You run inside autodev, an orchestrator. Its process rules in these instructions "
    "(the TDD phases, the orchestrator owns git, read-only roles, the structured output "
    "format) take precedence over any conflicting instruction in the project's CLAUDE.md, "
    "AGENTS.md, rules or skills. The project's conventions govern how the code is written "
    "(style, architecture, naming, testing practices)."
)


def load_instructions(role: Role, profile: AgentProfile, agent_dir: Path) -> str:
    return f"{_role_instructions(role, profile, agent_dir).rstrip()}\n\n{PRECEDENCE}"


def _role_instructions(role: Role, profile: AgentProfile, agent_dir: Path) -> str:
    if profile.prompt is not None:
        path = agent_dir / profile.prompt
        if not path.is_file():
            raise ConfigError(f"Prompt file for role {role.value} not found: {path}")
        return path.read_text()
    override = agent_dir / "process" / f"{role.value}.md"
    if override.is_file():
        return override.read_text()
    resource = files("autodev") / "prompts" / f"{role.value}.md"
    return resource.read_text()


def _criteria(story: Story) -> str:
    return "\n".join(f"- {c}" for c in story.acceptance_criteria)


def _story_section(story: Story) -> str:
    criteria = _criteria(story)
    return (
        f"Story {story.id}: {story.title}\n\n"
        f"Description:\n{story.description.strip()}\n\n"
        f"Acceptance criteria:\n{criteria}"
    )


def _context_section(context: list[Path]) -> str:
    listed = "\n".join(f"- {p}" for p in context) or "- (none)"
    return f"Project context files (read them first; paths relative to the project root):\n{listed}"


def _feedback_section(feedback: str | None) -> str:
    if not feedback:
        return ""
    tail = feedback[-MAX_FEEDBACK_CHARS:]
    return f"\n\nThe previous attempt failed. Feedback:\n{tail}"


def _task_prompt(
    intro: str,
    story: Story,
    task: Task,
    base_branch: str,
    context: list[Path],
    feedback: str | None,
) -> str:
    return (
        f"{intro}\n\n{_story_section(story)}\n\n"
        f"Task {task.id}: {task.title}\n\n"
        f"Base branch: {base_branch}\n\n{_context_section(context)}"
        f"{_feedback_section(feedback)}"
    )


def build_test_writer_prompt(
    story: Story, task: Task, base_branch: str, context: list[Path], feedback: str | None = None
) -> str:
    intro = "Write failing tests for this task only. Do not implement it."
    return _task_prompt(intro, story, task, base_branch, context, feedback)


def build_implementer_prompt(
    story: Story, task: Task, base_branch: str, context: list[Path], feedback: str | None = None
) -> str:
    intro = "Make the failing tests for this task pass with the minimum code. Do not touch tests."
    return _task_prompt(intro, story, task, base_branch, context, feedback)


def build_review_fix_prompt(
    story: Story,
    base_branch: str,
    context: list[Path],
    review_summary: str,
    findings: list[Finding],
    feedback: str | None = None,
) -> str:
    listed = "\n".join(f"- [{f.severity}] {f.file or '(general)'}: {f.message}" for f in findings)
    return (
        "The review of this story did not approve it. Address each finding below.\n\n"
        f"{_story_section(story)}\n\n"
        f"Base branch: {base_branch}\n\n"
        f"Review summary:\n{review_summary}\n\nFindings:\n{listed or '- (none)'}\n\n"
        f"{_context_section(context)}{_feedback_section(feedback)}"
    )


def build_reviewer_prompt(story: Story, base_branch: str, context: list[Path]) -> str:
    return (
        "Review the changes of this story. Use `git diff "
        f"{base_branch}...HEAD`.\n\n{_story_section(story)}\n\n"
        f"Base branch: {base_branch}\n\n{_context_section(context)}"
    )


def build_discovery_prompt(request: str, context: list[Path]) -> str:
    return (
        "Start the discovery briefing for this request. Read the project context first, "
        "then ask your first focused questions. Reply in the language of the request.\n\n"
        f"Request:\n{request.strip()}\n\n{_context_section(context)}"
    )


def build_refiner_prompt(story: Story, context: list[Path], feedback: str | None = None) -> str:
    prompt = (
        "Refine this story: user story, acceptance criteria (Given/When/Then) and a short "
        "ordered list of tasks, each small enough for one red-to-green TDD cycle. "
        "Use the same language as the request and the glossary terms. Honour the architecture "
        "decisions in the description: create tasks for each component it lists.\n\n"
        f"Story {story.id}: {story.title}\n\nDescription:\n{story.description.strip()}"
    )
    if story.acceptance_criteria or story.tasks:
        criteria = "\n".join(f"- {c}" for c in story.acceptance_criteria) or "- (none)"
        tasks = "\n".join(f"- {t.id}: {t.title}" for t in story.tasks) or "- (none)"
        prompt += f"\n\nCurrent acceptance criteria:\n{criteria}\n\nCurrent tasks:\n{tasks}"
    prompt += f"\n\n{_context_section(context)}"
    if feedback:
        prompt += f"\n\nFeedback to apply:\n{feedback[-MAX_FEEDBACK_CHARS:]}"
    return prompt


def build_qa_prompt(story: Story, base_branch: str, context: list[Path]) -> str:
    return (
        "Run QA on this story: verify every acceptance criterion with evidence. "
        f"The changes are in `git diff {base_branch}...HEAD`.\n\n"
        f"{_story_section(story)}\n\n"
        f"Base branch: {base_branch}\n\n{_context_section(context)}"
    )


def build_qa_fix_prompt(
    story: Story,
    base_branch: str,
    context: list[Path],
    qa: QaOutput,
    feedback: str | None = None,
) -> str:
    issues = "\n".join(f"- [{i.severity}] {i.message}" for i in qa.issues)
    unmet = "\n".join(f"- {c.criterion}: {c.evidence}" for c in qa.criteria if not c.met)
    return (
        "QA did not approve this story. Fix the issues and the unmet acceptance criteria below.\n\n"
        f"{_story_section(story)}\n\n"
        f"Base branch: {base_branch}\n\n"
        f"QA summary:\n{qa.summary}\n\nIssues:\n{issues or '- (none)'}\n\n"
        f"Unmet criteria:\n{unmet or '- (none)'}\n\n"
        f"{_context_section(context)}{_feedback_section(feedback)}"
    )


def build_merge_prompt(story: Story, conflicted_files: list[str], feedback: str | None) -> str:
    listed = "\n".join(f"- {f}" for f in conflicted_files)
    return (
        "Rebasing this story branch onto the main branch produced conflicts. Resolve the "
        "conflict markers in each file below, keeping the intent of both sides and the story's "
        "acceptance criteria.\n\n"
        f"Story {story.id}: {story.title}\n\n"
        f"Acceptance criteria:\n{_criteria(story)}\n\n"
        f"Conflicted files:\n{listed}{_feedback_section(feedback)}"
    )


def build_merge_fix_prompt(
    story: Story, base_branch: str, context: list[Path], feedback: str | None
) -> str:
    return (
        "The quality gates fail after rebasing this story onto the main branch. "
        "Fix what the output below reports.\n\n"
        f"{_story_section(story)}\n\n"
        f"Base branch: {base_branch}\n\n"
        f"{_context_section(context)}{_feedback_section(feedback)}"
    )


def gate_feedback(report: GateReport) -> str:
    return "\n".join(f"Gate {r.name} failed:\n{r.output}" for r in report.failures)


def build_brief_prompt(context: list[Path], empty: bool) -> str:
    state = (
        "The repository is empty: the stack and architecture are still to be decided."
        if empty
        else "The repository already has code: read it and document what exists."
    )
    return (
        "Start the technical briefing. Define the stack (language, framework), the architecture "
        "style, the testing and lint tools, the build/test/lint shell commands and any "
        "constraints. For an existing codebase, read CLAUDE.md, AGENTS.md and .claude/rules/ "
        "first and do not restate conventions already documented there: the standards should "
        "only add what is missing. Ask a few focused questions per turn. Finish with done=true "
        "and the TechBrief (summary, ADRs, commands, standards). "
        "Reply in the language of the user.\n\n"
        f"{state}\n\n{_context_section(context)}"
    )


def build_brief_feedback_prompt(previous: TechBrief, feedback: str) -> str:
    return (
        "The user reviewed your technical brief and asked for changes. Continue the briefing, "
        "apply the feedback and finish with done=true and the updated TechBrief.\n\n"
        f"Previous proposal (JSON):\n{previous.model_dump_json(indent=2)}\n\n"
        f"Feedback:\n{feedback[-MAX_FEEDBACK_CHARS:]}"
    )


def build_architecture_review_prompt(
    story: Story,
    result: DiscoveryResult,
    context: list[Path],
    previous: ArchitectureReview | None = None,
    feedback: str | None = None,
) -> str:
    prompt = (
        "Review the architecture for this story: propose the decisions (ADRs), the components "
        "and the design notes it needs. Stay consistent with the existing ADRs.\n\n"
        f"Story {story.id}: {story.title}\n\nProblem:\n{result.problem.strip()}\n\n"
        f"Architecture notes from discovery:\n{result.architecture_notes.strip() or '(none)'}\n\n"
        f"Domain events: {', '.join(result.event_storming.domain_events) or '(none)'}\n"
        f"Aggregates: {', '.join(result.event_storming.aggregates) or '(none)'}\n\n"
        f"{_context_section(context)}"
    )
    if previous is not None:
        prompt += f"\n\nPrevious proposal (JSON):\n{previous.model_dump_json(indent=2)}"
    if feedback:
        prompt += f"\n\nFeedback to apply:\n{feedback[-MAX_FEEDBACK_CHARS:]}"
    return prompt


def build_bootstrap_prompt(brief: TechBrief, context: list[Path], feedback: str | None) -> str:
    commands = brief.commands
    standards = "\n".join(f"- {s}" for s in brief.standards) or "- (none)"
    adrs = "\n".join(f"- {a.title}: {a.decision}" for a in brief.adrs)
    return (
        "Create the project skeleton for this empty repository, following the ADRs and "
        "standards below. Configure the test runner and the linters so that the configured "
        "commands work, and add one smoke test that passes. Do not implement features.\n\n"
        f"Summary:\n{brief.summary}\n\nDecisions:\n{adrs}\n\nStandards:\n{standards}\n\n"
        f"Commands (must work when you finish):\n- build: {commands.build}\n"
        f"- test: {commands.test}\n- lint: {commands.lint}\n\n"
        f"{_context_section(context)}{_feedback_section(feedback)}"
    )


def build_process_retro_prompt(
    stats_markdown: str, effective_instructions: dict[Role, str], context: list[Path]
) -> str:
    instructions = "\n\n".join(
        f"### {role.value}\n{text.strip()}" for role, text in effective_instructions.items()
    )
    return (
        "Run a process retrospective. Below are the metrics computed from the run logs and the "
        "instructions each role currently uses. Propose few, high-value changes: a `prompt` "
        "proposal (target = role name, content = the complete new instructions), a `standard` "
        "proposal (target = standards file name such as testing.md, content = markdown to "
        "append) or a `config` recommendation (target = config key, content = what to change). "
        "Base each proposal on evidence from the metrics.\n\n"
        f"# Metrics\n{stats_markdown}\n\n# Current instructions per role\n{instructions}\n\n"
        f"{_context_section(context)}"
    )


def build_code_scan_prompt(churn: list[tuple[str, int]], context: list[Path]) -> str:
    listed = "\n".join(f"- {path}: {count} commits" for path, count in churn) or "- (none)"
    return (
        "Scan the code base for refactoring opportunities. Propose small, self-contained "
        "refactorings, each one a story, aligned with the standards and ADRs. Prioritise the "
        "files that change most often.\n\n"
        f"Most changed files:\n{listed}\n\n{_context_section(context)}"
    )


def build_lint_prompt(context: list[Path], lint_command: str | None) -> str:
    return (
        "Lint mode: propose lint rules that enforce the project standards. Only propose rules "
        "for standards that a linter can check mechanically, using tools that fit the stack "
        "described in the ADRs (prefer the tools already configured). Make each rule concrete "
        "(the exact rule id or config snippet) and say which standard it enforces. Never "
        "duplicate rules that are already enabled: read the project's lint config files first.\n\n"
        f"Current lint command: {lint_command or 'none configured'}\n\n{_context_section(context)}"
    )
