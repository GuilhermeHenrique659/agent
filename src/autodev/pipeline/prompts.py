from importlib.resources import files
from pathlib import Path

from autodev.config.loader import ConfigError
from autodev.config.models import AgentProfile
from autodev.domain.role import Role
from autodev.domain.story import Story, Task
from autodev.gates.runner import GateReport
from autodev.pipeline.outputs import Finding, QaOutput

MAX_FEEDBACK_CHARS = 8000


def load_instructions(role: Role, profile: AgentProfile, agent_dir: Path) -> str:
    if profile.prompt is not None:
        path = agent_dir / profile.prompt
        if not path.is_file():
            raise ConfigError(f"Prompt file for role {role.value} not found: {path}")
        return path.read_text()
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
        "Use the same language as the request and the glossary terms.\n\n"
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
