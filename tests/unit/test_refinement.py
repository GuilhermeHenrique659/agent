import asyncio
from pathlib import Path

import pytest

from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus
from autodev.pipeline.outputs import RefinedTask, RefinementOutput
from autodev.pipeline.refinement import refine_story
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, Step, agent_error


class Context:
    def paths(self) -> list[Path]:
        return [Path(".agent/domain/glossary.md")]


def output(title: str = "Refined") -> RefinementOutput:
    return RefinementOutput(
        title=title,
        user_story="As a buyer, I want to pay",
        acceptance_criteria=["Given a cart When paid Then done"],
        tasks=[RefinedTask(title="first"), RefinedTask(title="second")],
    )


def make_story() -> Story:
    return Story("S-0001", "t", description="## Request\nraw\n", status=StoryStatus.DISCOVERED)


def refine(
    tmp_path: Path, runtime: FakeAgentRuntime, story: Story, feedback: str | None = None
) -> Story:
    return asyncio.run(
        refine_story(
            story,
            config=ProjectConfig(),
            project_root=tmp_path,
            runtime=runtime,
            context=Context(),
            log=RunLog(tmp_path, story.id),
            feedback=feedback,
        )
    )


def test_refine_applies_output_to_story(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    runtime.script(Role.REFINER, Step(output()))
    story = refine(tmp_path, runtime, make_story())
    assert story.status == StoryStatus.REFINED
    assert story.title == "Refined"
    assert story.description == "## User story\nAs a buyer, I want to pay\n\n## Request\nraw\n"
    assert story.acceptance_criteria == ["Given a cart When paid Then done"]
    assert [(t.id, t.title) for t in story.tasks] == [("T1", "first"), ("T2", "second")]
    assert ".agent/domain/glossary.md" in runtime.requests[0].prompt


def test_feedback_and_current_tasks_in_prompt_when_re_refining(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    runtime.script(Role.REFINER, Step(output()), Step(output("Again")))
    story = refine(tmp_path, runtime, make_story())
    story = refine(tmp_path, runtime, story, feedback="split the second task")
    prompt = runtime.requests[1].prompt
    assert "split the second task" in prompt
    assert "T2: second" in prompt
    assert "Given a cart When paid Then done" in prompt
    assert "Feedback" not in runtime.requests[0].prompt
    assert story.description.count("## User story") == 1


def test_agent_error_becomes_run_error(tmp_path: Path) -> None:
    runtime = FakeAgentRuntime()
    runtime.script(Role.REFINER, Step(agent_error("boom")))
    story = make_story()
    with pytest.raises(RunError, match="boom"):
        refine(tmp_path, runtime, story)
    assert story.status == StoryStatus.DISCOVERED
