from collections.abc import Callable
from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.domain.story import Story
from autodev.pipeline.architecture import review_architecture
from autodev.pipeline.discovery import run_discovery
from autodev.pipeline.domain_docs import append_event_storming, merge_glossary
from autodev.pipeline.refinement import refine_story
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_docs import render_discovery
from autodev.ports.agent_runtime import AgentRuntime
from autodev.ports.context_source import ContextSource
from autodev.ports.user_io import UserIO

_TITLE_CHARS = 60


async def handle_request(
    text: str,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    store: StoryStore,
    context: ContextSource,
    io: UserIO,
    log_factory: Callable[[str], RunLog],
) -> Story:
    """Take a request through discovery and refinement; the story is saved after every step."""
    story = Story(store.next_id(), _first_line(text)[:_TITLE_CHARS], description=text)
    store.save(story)
    return await discover_and_refine(
        story,
        config=config,
        project_root=project_root,
        runtime=runtime,
        store=store,
        context=context,
        io=io,
        log=log_factory(story.id),
    )


async def discover_and_refine(
    story: Story,
    *,
    config: ProjectConfig,
    project_root: Path,
    runtime: AgentRuntime,
    store: StoryStore,
    context: ContextSource,
    io: UserIO,
    log: RunLog,
) -> Story:
    """Discovery (the story description is the request) -> domain docs -> architecture review
    (when flagged) -> refinement."""
    text = story.description
    result = await run_discovery(
        story,
        config=config,
        project_root=project_root,
        runtime=runtime,
        context=context,
        io=io,
        log=log,
    )
    domain = project_root / ".agent" / "domain"
    merge_glossary(domain / "glossary.md", result.glossary)
    append_event_storming(domain / "events.md", story.id, result.title, result.event_storming)
    description = render_discovery(text, result)
    if result.needs_architecture_review:
        section = await review_architecture(
            story,
            result,
            config=config,
            project_root=project_root,
            runtime=runtime,
            context=context,
            io=io,
            log=log,
        )
        description += "\n" + section
    story.record_discovery(result.title, description)
    store.save(story)
    await refine_story(
        story,
        config=config,
        project_root=project_root,
        runtime=runtime,
        context=context,
        log=log,
    )
    store.save(story)
    return story


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""
