import asyncio
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config.models import CommandsConfig, LimitsConfig, ProjectConfig
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.pipeline.outputs import Finding, ImplementerOutput, ReviewOutput, TestWriterOutput
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError, StoryRunner
from tests.fakes import FakeAgentRuntime, Step, agent_error
from tests.integration.conftest import git
from tests.unit.test_tdd import CHECK, IMPL_BAD, IMPL_GOOD

BAD_TESTS = CHECK + "# edit\n"


@dataclass
class Env:
    root: Path
    repo: GitRepo
    store: StoryStore
    runtime: FakeAgentRuntime
    runner: StoryRunner


def writer(files: dict[str, str]) -> Callable[[Path], None]:
    def edit(workdir: Path) -> None:
        for name, content in files.items():
            path = workdir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    return edit


def tw(files: dict[str, str] | None = None) -> Step:
    edit = writer(files if files is not None else {"tests/check.py": CHECK})
    return Step(TestWriterOutput(summary="tests", test_files=["tests/check.py"]), edit)


def impl(files: dict[str, str] | None = None) -> Step:
    edit = writer(files if files is not None else {"impl.py": IMPL_GOOD})
    return Step(ImplementerOutput(summary="done"), edit)


def approve() -> Step:
    return Step(ReviewOutput(approved=True, summary="lgtm", findings=[]))


def reject(message: str = "rename it") -> Step:
    findings = [Finding(severity="blocking", file="impl.py", message=message)]
    return Step(ReviewOutput(approved=False, summary="needs work", findings=findings))


def make_env(
    tmp_path: Path,
    *,
    lint: str | None = None,
    max_attempts: int = 3,
    status: StoryStatus = StoryStatus.APPROVED,
    tasks: list[Task] | None = None,
    gitignore: bool = True,
) -> Env:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    (root / ".agent").mkdir()
    (root / ".agent" / "config.yaml").write_text("")
    if gitignore:
        (root / ".agent" / ".gitignore").write_text("backlog/\nruns/\n")
    (root / "impl.py").write_text(IMPL_BAD)
    story = Story(
        "S-0001",
        "Say two",
        description="Return two.",
        status=status,
        acceptance_criteria=["value() is 2"],
        tasks=tasks if tasks is not None else [Task("T1", "Say two")],
    )
    store = StoryStore(root)
    store.save(story)
    repo.commit_all("initial")
    command = f"{shlex.quote(sys.executable)} -B tests/check.py"
    config = ProjectConfig(
        commands=CommandsConfig(test=command, lint=lint),
        limits=LimitsConfig(max_attempts=max_attempts),
    )
    runtime = FakeAgentRuntime()
    runner = StoryRunner(
        config, root, runtime, repo, store, KnowledgeFolder(root), lambda sid: RunLog(root, sid)
    )
    return Env(root, repo, store, runtime, runner)


def run(env: Env) -> Story:
    return asyncio.run(env.runner.run("S-0001"))


def subjects(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s", "main..HEAD"], cwd=root, capture_output=True, text=True
    ).stdout
    return out.splitlines()


def test_happy_path_reaches_in_qa_with_red_and_green_commits(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    assert story.tasks[0].status == TaskStatus.DONE
    assert env.store.get("S-0001").status == StoryStatus.IN_QA
    assert env.repo.current_branch() == "story/S-0001"
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    reviewer_prompt = env.runtime.requests_for(Role.REVIEWER)[0].prompt
    assert "main...HEAD" in reviewer_prompt


@pytest.mark.parametrize("status", [StoryStatus.DRAFT, StoryStatus.BLOCKED])
def test_rejects_story_not_approved(tmp_path: Path, status: StoryStatus) -> None:
    env = make_env(tmp_path, status=status)
    with pytest.raises(RunError, match=status.value):
        run(env)
    assert env.runtime.requests == []


def test_rejects_dirty_working_tree(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    (env.root / "stray.txt").write_text("x")
    with pytest.raises(RunError, match="uncommitted"):
        run(env)
    assert env.runtime.requests == []


def test_rejects_when_agent_state_not_gitignored(tmp_path: Path) -> None:
    env = make_env(tmp_path, gitignore=False)
    with pytest.raises(RunError, match=r"\.agent/\.gitignore"):
        run(env)
    assert env.runtime.requests == []


def test_red_violation_resets_and_retries_with_feedback(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(
        Role.TEST_WRITER,
        tw({"tests/check.py": CHECK, "impl.py": IMPL_GOOD}),
        tw(),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    first, second = env.runtime.requests_for(Role.TEST_WRITER)
    assert "previous attempt" not in first.prompt
    assert "may only touch test files" in second.prompt
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]


def test_green_touching_tests_resets_and_retries(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(
        Role.IMPLEMENTER,
        impl({"impl.py": IMPL_GOOD, "tests/check.py": BAD_TESTS}),
        impl(),
    )
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    assert "may not touch test files" in env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    assert (env.root / "tests" / "check.py").read_text() == CHECK


def test_green_failing_tests_keeps_commit_and_fixes_on_top(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl({"impl.py": IMPL_BAD + "# wrong\n"}), impl())
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    assert "Tests failed" in env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert subjects(env.root) == [
        "fix(T1): Say two",
        "feat(T1): Say two",
        "test(T1): Say two",
    ]


def test_gate_failure_feeds_output_back_to_implementer(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint="test -f ok.txt || (echo lint-missing-ok; exit 1)")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"ok.txt": "ok"}))
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    second = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "lint" in second and "lint-missing-ok" in second
    assert subjects(env.root)[0] == "fix(T1): Say two"


def test_blocks_story_when_attempts_exhausted(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(
        Role.IMPLEMENTER,
        Step(ImplementerOutput(summary="nothing")),
        Step(ImplementerOutput(summary="nothing")),
    )
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None and "no changes" in story.blocked_reason
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 2
    assert env.runtime.requests_for(Role.REVIEWER) == []
    assert env.store.get("S-0001").status == StoryStatus.BLOCKED
    assert story.tasks[0].status == TaskStatus.PENDING


def test_agent_error_counts_as_failed_attempt(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    junk = writer({"junk.txt": "x"})
    env.runtime.script(Role.TEST_WRITER, Step(agent_error("model down"), junk), tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    assert "model down" in env.runtime.requests_for(Role.TEST_WRITER)[1].prompt
    assert not (env.root / "junk.txt").exists()

    blocked = make_env(tmp_path / "b", max_attempts=1)
    blocked.runtime.script(Role.TEST_WRITER, Step(agent_error("model down")))
    result = run(blocked)
    assert result.status == StoryStatus.BLOCKED
    assert result.blocked_reason is not None and "model down" in result.blocked_reason


def test_review_rejection_sends_back_then_approves(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"notes.txt": "fixed"}))
    env.runtime.script(Role.REVIEWER, reject("rename it"), approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    assert story.attempts == 1
    fix_prompt = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "rename it" in fix_prompt and "needs work" in fix_prompt
    assert subjects(env.root)[0] == "fix(review): address review findings"
    assert len(env.runtime.requests_for(Role.REVIEWER)) == 2


def test_review_rejections_block_after_max_attempts(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"notes.txt": "fixed"}))
    env.runtime.script(Role.REVIEWER, reject(), reject())
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason == "needs work"
    assert len(env.runtime.requests_for(Role.REVIEWER)) == 2
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 2
    assert env.store.get("S-0001").status == StoryStatus.BLOCKED


def test_resume_skips_done_tasks(tmp_path: Path) -> None:
    tasks = [Task("T1", "First", TaskStatus.DONE), Task("T2", "Second")]
    env = make_env(tmp_path, status=StoryStatus.IN_PROGRESS, tasks=tasks)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    story = run(env)
    assert story.status == StoryStatus.IN_QA
    (request,) = env.runtime.requests_for(Role.TEST_WRITER)
    assert "Task T2: Second" in request.prompt
    assert subjects(env.root) == ["feat(T2): Second", "test(T2): Second"]


def test_in_qa_story_is_returned_unchanged(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.IN_QA)
    assert run(env).status == StoryStatus.IN_QA
    assert env.runtime.requests == []


def test_run_log_records_events(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint="exit 0")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    run(env)
    (log_file,) = (env.root / ".agent" / "runs" / "S-0001").glob("*.jsonl")
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    events = [r["event"] for r in records]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    for expected in ("agent_run", "tdd_red", "tdd_green", "gate", "review"):
        assert expected in events
    agent_runs = [r for r in records if r["event"] == "agent_run"]
    assert [r["role"] for r in agent_runs] == ["test_writer", "implementer", "reviewer"]
    assert agent_runs[0]["task"] == "T1"
    assert agent_runs[0]["ok"] is True
    assert agent_runs[0]["cost_usd"] == 0.01
    assert not env.repo.has_changes()
