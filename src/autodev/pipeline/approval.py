from pathlib import Path

from autodev.adapters.git import GitError, GitRepo
from autodev.config.models import ProjectConfig
from autodev.domain.story import InvalidTransition, Story, StoryStatus
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError

_DOC_PATHS = [".agent/domain", ".agent/architecture"]


def approve_story(
    story_id: str,
    *,
    config: ProjectConfig,
    project_root: Path,
    repo: GitRepo,
    store: StoryStore,
) -> Story:
    try:
        story = store.get(story_id)
    except KeyError as exc:
        raise RunError(f"Story {story_id} not found") from exc
    main = config.git.main_branch
    if (branch := repo.current_branch()) != main:
        raise RunError(f"Approval must happen on {main}, but the current branch is {branch}")
    try:
        story.advance(StoryStatus.APPROVED)
    except InvalidTransition as exc:
        raise RunError(
            f"Story {story.id} is {story.status.value}; only refined stories: {exc}"
        ) from exc
    # Commit the docs before persisting APPROVED, so a failed commit leaves the story refined.
    try:
        repo.commit_paths(_DOC_PATHS, f"docs({story.id}): discovery and refinement")
    except GitError as exc:
        raise RunError(str(exc)) from exc
    store.save(story)
    return story
