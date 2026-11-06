import pytest

from autodev.domain.story import InvalidTransition, Story, StoryStatus, Task, TaskStatus

S = StoryStatus


def make_story(status: StoryStatus = S.DRAFT, *, tasks_done: bool = True) -> Story:
    task = Task("T-1", "t", TaskStatus.DONE if tasks_done else TaskStatus.PENDING)
    return Story("S-0001", "title", status=status, acceptance_criteria=["ac"], tasks=[task])


def test_advance_follows_forward_chain_to_done() -> None:
    story = make_story()
    for step in [
        S.DISCOVERED, S.REFINED, S.APPROVED, S.IN_PROGRESS, S.IN_REVIEW, S.IN_QA, S.DONE,
    ]:  # fmt: skip
        story.advance(step)
        assert story.status == step


def test_advance_skipping_a_step_raises() -> None:
    story = make_story()
    with pytest.raises(InvalidTransition):
        story.advance(S.REFINED)


def test_advance_to_blocked_raises() -> None:
    story = make_story()
    with pytest.raises(InvalidTransition):
        story.advance(S.BLOCKED)


def test_approve_without_tasks_raises() -> None:
    story = make_story(S.REFINED)
    story.tasks = []
    with pytest.raises(InvalidTransition):
        story.advance(S.APPROVED)


def test_approve_without_acceptance_criteria_raises() -> None:
    story = make_story(S.REFINED)
    story.acceptance_criteria = []
    with pytest.raises(InvalidTransition):
        story.advance(S.APPROVED)


def test_review_with_pending_task_raises() -> None:
    story = make_story(S.IN_PROGRESS, tasks_done=False)
    with pytest.raises(InvalidTransition):
        story.advance(S.IN_REVIEW)


def test_send_back_returns_to_in_progress_and_counts_attempt() -> None:
    story = make_story(S.IN_REVIEW)
    story.send_back("nope", max_attempts=3)
    assert story.status == S.IN_PROGRESS
    assert story.attempts == 1


def test_send_back_blocks_when_max_attempts_reached() -> None:
    story = make_story(S.IN_QA)
    story.attempts = 2
    story.send_back("still bad", max_attempts=3)
    assert story.status == S.BLOCKED
    assert story.attempts == 3
    assert story.blocked_from == S.IN_QA
    assert story.blocked_reason == "still bad"


def test_send_back_from_other_status_raises() -> None:
    story = make_story(S.IN_PROGRESS)
    with pytest.raises(InvalidTransition):
        story.send_back("x", max_attempts=3)


def test_block_records_origin_and_reason() -> None:
    story = make_story(S.REFINED)
    story.block("waiting")
    assert story.status == S.BLOCKED
    assert story.blocked_from == S.REFINED
    assert story.blocked_reason == "waiting"


def test_block_done_story_raises() -> None:
    story = make_story(S.DONE)
    with pytest.raises(InvalidTransition):
        story.block("x")


def test_resume_restores_previous_status_and_resets_attempts() -> None:
    story = make_story(S.REFINED)
    story.attempts = 2
    story.block("waiting")
    story.resume()
    assert story.status == S.REFINED
    assert story.attempts == 0
    assert story.blocked_from is None
    assert story.blocked_reason is None


def test_resume_after_send_back_block_goes_to_in_progress() -> None:
    story = make_story(S.IN_REVIEW)
    story.send_back("bad", max_attempts=1)
    assert story.blocked_from == S.IN_REVIEW
    story.resume()
    assert story.status == S.IN_PROGRESS
    assert story.attempts == 0


def test_resume_when_not_blocked_raises() -> None:
    story = make_story(S.REFINED)
    with pytest.raises(InvalidTransition):
        story.resume()


def test_done_is_terminal() -> None:
    story = make_story(S.DONE)
    with pytest.raises(InvalidTransition):
        story.advance(S.DONE)
    with pytest.raises(InvalidTransition):
        story.send_back("x", max_attempts=3)
    with pytest.raises(InvalidTransition):
        story.resume()


def test_complete_task_twice_raises() -> None:
    task = Task("T-1", "t")
    task.complete()
    assert task.status == TaskStatus.DONE
    with pytest.raises(InvalidTransition):
        task.complete()
