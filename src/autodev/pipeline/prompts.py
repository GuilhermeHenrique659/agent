from importlib.resources import files
from pathlib import Path

from autodev.config.loader import ConfigError
from autodev.config.models import AgentProfile
from autodev.domain.role import Role
from autodev.domain.story import Story, Task
from autodev.pipeline.outputs import Finding

MAX_FEEDBACK_CHARS = 8000


def load_instructions(role: Role, profile: AgentProfile, agent_dir: Path) -> str:
    if profile.prompt is not None:
        path = agent_dir / profile.prompt
        if not path.is_file():
            raise ConfigError(f"Prompt file for role {role.value} not found: {path}")
        return path.read_text()
    resource = files("autodev") / "prompts" / f"{role.value}.md"
    return resource.read_text()


def _story_section(story: Story) -> str:
    criteria = "\n".join(f"- {c}" for c in story.acceptance_criteria)
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
