import asyncio
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config.models import (
    CommandsConfig,
    GitConfig,
    LimitsConfig,
    ProjectConfig,
    TddConfig,
)
from autodev.domain.role import Role
from autodev.domain.story import Story, StoryStatus, Task, TaskStatus
from autodev.pipeline.outputs import (
    CriterionCheck,
    CriterionCoverage,
    Finding,
    ImplementerOutput,
    Improvement,
    MergeOutput,
    QaIssue,
    QaOutput,
    ReviewOutput,
    ScaffoldOutput,
    TestWriterOutput,
    UncoveredCriterion,
)
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError, StoryRunner
from tests.fakes import FakeAgentRuntime, Step, agent_error, write_formatter
from tests.integration.conftest import git
from tests.unit.test_tdd import CHECK, IMPL_BAD, IMPL_GOOD

BAD_TESTS = CHECK + "# edit\n"
JUNIT_REPORT = ".agent/runs/junit.xml"
# A tiny runner that writes a JUnit XML report: it runs the test_* functions of tests/test_*.py,
# reports an import failure as a collection error and exits non-zero unless all passed.
JUNIT_RUNNER = """\
import importlib.util
import sys
import traceback
import unittest
from pathlib import Path
from xml.sax.saxutils import quoteattr

root = Path.cwd()
sys.path.insert(0, str(root))
cases = []
for file in sorted((root / "tests").glob("test_*.py")):
    module_name = "tests." + file.stem
    try:
        spec = importlib.util.spec_from_file_location(file.stem, file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        cases.append(("", module_name, "error", traceback.format_exc()))
        continue
    for name in sorted(dir(module)):
        function = getattr(module, name)
        if not name.startswith("test_") or not callable(function):
            continue
        try:
            function()
        except unittest.SkipTest as skip:
            cases.append((module_name, name, "skipped", str(skip)))
        except BaseException:
            cases.append((module_name, name, "failure", traceback.format_exc()))
        else:
            cases.append((module_name, name, "passed", ""))
lines = ["<testsuite>"]
bad = False
for classname, name, kind, text in cases:
    head = "<testcase classname=" + quoteattr(classname) + " name=" + quoteattr(name)
    if kind == "passed":
        lines.append(head + "/>")
        continue
    if kind != "skipped":
        bad = True
        print(kind.upper(), classname, name)
        print(text)
    lines.append(head + "><" + kind + " message=" + quoteattr(text[-200:]) + "/></testcase>")
lines.append("</testsuite>")
report = Path(sys.argv[1])
report.parent.mkdir(parents=True, exist_ok=True)
report.write_text("\\n".join(lines))
sys.exit(1 if bad else 0)
"""


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
    return Step(
        ReviewOutput(approved=True, summary="lgtm", findings=[], test_changes_confirmed=True)
    )


def reject(message: str = "rename it") -> Step:
    findings = [Finding(severity="blocking", file="impl.py", message=message)]
    return Step(ReviewOutput(approved=False, summary="needs work", findings=findings))


def qa_ok(improvements: list[Improvement] | None = None) -> Step:
    criteria = [CriterionCheck(criterion="value() is 2", met=True, evidence="check passes")]
    out = QaOutput(
        approved=True, summary="all good", criteria=criteria, improvements=improvements or []
    )
    return Step(out)


def qa_no(message: str = "value is wrong") -> Step:
    criteria = [CriterionCheck(criterion="value() is 2", met=False, evidence="returns 3")]
    issues = [QaIssue(severity="blocking", message=message)]
    return Step(QaOutput(approved=False, summary="qa failed", criteria=criteria, issues=issues))


def merger(files: dict[str, str] | None = None) -> Step:
    edit = writer(files) if files is not None else None
    return Step(MergeOutput(summary="resolved"), edit)


def make_env(
    tmp_path: Path,
    *,
    lint: str | None = None,
    max_attempts: int = 3,
    status: StoryStatus = StoryStatus.APPROVED,
    tasks: list[Task] | None = None,
    gitignore: bool = True,
    remote: str | None = None,
    format: str | None = None,
    files: dict[str, str] | None = None,
    junit: bool = False,
    junit_path: str = JUNIT_REPORT,
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
    writer({**({"runner.py": JUNIT_RUNNER} if junit else {}), **(files or {})})(root)
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
    script = f"runner.py {junit_path}" if junit else "tests/check.py"
    command = f"{shlex.quote(sys.executable)} -B {script}"
    config = ProjectConfig(
        commands=CommandsConfig(test=command, lint=lint, format=format),
        tdd=TddConfig(junit_report=junit_path if junit else None),
        limits=LimitsConfig(max_attempts=max_attempts),
        git=GitConfig(remote=remote),
    )
    runtime = FakeAgentRuntime()
    runner = StoryRunner(
        config, root, runtime, repo, store, KnowledgeFolder(root), lambda sid: RunLog(root, sid)
    )
    return Env(root, repo, store, runtime, runner)


def run(env: Env) -> Story:
    return asyncio.run(env.runner.run("S-0001"))


def subjects(root: Path, rev: str = "HEAD") -> list[str]:
    """Commit subjects reachable from `rev`, newest first, without the initial commit."""
    out = subprocess.run(
        ["git", "log", "--format=%s", rev], cwd=root, capture_output=True, text=True
    ).stdout
    return out.splitlines()[:-1]


def branch_names(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"], cwd=root, capture_output=True, text=True
    ).stdout
    return out.split()


def test_full_run_reaches_done_merges_into_main_and_deletes_branch(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert story.tasks[0].status == TaskStatus.DONE
    assert env.store.get("S-0001").status == StoryStatus.DONE
    assert env.repo.current_branch() == "main"
    assert branch_names(env.root) == ["main"]
    assert (env.root / "impl.py").read_text() == IMPL_GOOD
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    reviewer_prompt = env.runtime.requests_for(Role.REVIEWER)[0].prompt
    assert "main...HEAD" in reviewer_prompt
    assert "main...HEAD" in env.runtime.requests_for(Role.QA)[0].prompt


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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert "may not touch test files" in env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    assert (env.root / "tests" / "check.py").read_text() == CHECK


def test_green_failing_tests_keeps_commit_and_fixes_on_top(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl({"impl.py": IMPL_BAD + "# wrong\n"}), impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    second = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "lint" in second and "lint-missing-ok" in second
    assert subjects(env.root)[0] == "fix(T1): Say two"


def test_implement_no_change_after_kept_commit_reruns_gates(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint="test -f .agent/runs/ready || (echo lint-flag-missing; exit 1)")
    env.runtime.script(Role.TEST_WRITER, tw())
    flag = Step(ImplementerOutput(summary="nothing to change"), writer({".agent/runs/ready": "ok"}))
    env.runtime.script(Role.IMPLEMENTER, impl(), flag)
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 2
    assert subjects(env.root)[0] == "feat(T1): Say two"


def test_implement_no_change_failure_carries_gate_output(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2, lint="echo lint-flag-missing; exit 1")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), Step(ImplementerOutput(summary="nothing")))
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None
    assert "made no changes, but the gates still fail" in story.blocked_reason
    assert "lint-flag-missing" in story.blocked_reason


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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
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
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    (request,) = env.runtime.requests_for(Role.TEST_WRITER)
    assert "Task T2: Second" in request.prompt
    assert subjects(env.root) == ["feat(T2): Second", "test(T2): Second"]


def test_done_story_is_returned_unchanged(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.DONE)
    assert run(env).status == StoryStatus.DONE
    assert env.runtime.requests == []


def test_run_log_records_events(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint="exit 0")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    run(env)
    (log_file,) = (env.root / ".agent" / "runs" / "S-0001").glob("*.jsonl")
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    events = [r["event"] for r in records]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    for expected in ("agent_run", "tdd_red", "tdd_green", "gate", "review", "qa", "merged"):
        assert expected in events
    agent_runs = [r for r in records if r["event"] == "agent_run"]
    assert [r["role"] for r in agent_runs] == ["test_writer", "implementer", "reviewer", "qa"]
    assert agent_runs[0]["task"] == "T1"
    assert agent_runs[0]["ok"] is True
    assert agent_runs[0]["cost_usd"] == 0.01
    assert not env.repo.has_changes()


def done_tasks() -> list[Task]:
    return [Task("T1", "Say two", TaskStatus.DONE)]


def green_main(env: Env) -> None:
    """Make the project's gates pass on main (for stories that enter at QA with no commits)."""
    writer({"tests/check.py": CHECK, "impl.py": IMPL_GOOD})(env.root)
    env.repo.commit_all("green main")


def test_in_qa_story_runs_qa_and_merge(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.IN_QA, tasks=done_tasks())
    green_main(env)
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert env.runtime.requests_for(Role.REVIEWER) == []
    assert env.runtime.requests_for(Role.IMPLEMENTER) == []
    assert len(env.runtime.requests_for(Role.QA)) == 1
    assert branch_names(env.root) == ["main"]


def test_qa_rejection_sends_back_fixes_then_reviews_and_qa_again(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"notes.txt": "qa fix"}))
    env.runtime.script(Role.REVIEWER, approve(), approve())
    env.runtime.script(Role.QA, qa_no("value is wrong"), qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert story.attempts == 1
    fix_prompt = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert (
        "value is wrong" in fix_prompt and "qa failed" in fix_prompt and "returns 3" in fix_prompt
    )
    assert subjects(env.root)[0] == "fix(qa): address QA findings"
    assert len(env.runtime.requests_for(Role.REVIEWER)) == 2
    assert len(env.runtime.requests_for(Role.QA)) == 2


def test_qa_rejections_share_attempts_with_review_and_block(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"notes.txt": "fixed"}))
    env.runtime.script(Role.REVIEWER, reject(), approve())
    env.runtime.script(Role.QA, qa_no())
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason == "qa failed"
    assert story.attempts == 2
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 2
    assert env.store.get("S-0001").status == StoryStatus.BLOCKED
    assert env.repo.current_branch() == "story/S-0001"


def test_qa_improvements_become_draft_stories(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.IN_QA, tasks=done_tasks())
    green_main(env)
    improvements = [
        Improvement(title="x" * 80, description="Add logging"),
        Improvement(title="Cache it", description="Cache values"),
    ]
    env.runtime.script(Role.QA, qa_ok(improvements))
    assert run(env).status == StoryStatus.DONE
    first = env.store.get("S-0002")
    second = env.store.get("S-0003")
    assert first.status == StoryStatus.DRAFT
    assert first.title == "x" * 60
    assert first.description == "Add logging\n\nSuggested by QA while delivering S-0001."
    assert second.title == "Cache it"
    log_file = next((env.root / ".agent" / "runs" / "S-0001").glob("*.jsonl"))
    records = [json.loads(line) for line in log_file.read_text().splitlines()]
    created = [r["detail"] for r in records if r["event"] == "improvement_created"]
    assert created == ["S-0002", "S-0003"]


def test_qa_agent_error_blocks_story(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.IN_QA, tasks=done_tasks())
    env.runtime.script(Role.QA, Step(agent_error("qa down")))
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None
    assert story.blocked_reason.startswith("QA failed.") and "qa down" in story.blocked_reason


def conflicting_main(env: Env, files: dict[str, str]) -> None:
    """Pre-create the story branch, then commit `files` on main so the branch is behind."""
    env.repo.create_branch("story/S-0001", "main")
    env.repo.checkout("main")
    for name, content in files.items():
        (env.root / name).write_text(content)
    env.repo.commit_all("main moves on")


MAIN_IMPL = "def value():\n    return 1  # main\n"
RESOLVED_IMPL = "def value():\n    return 2  # main\n"


def test_merge_conflict_resolved_by_merger_agent(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    conflicting_main(env, {"impl.py": MAIN_IMPL, "main_note.txt": "from main"})
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    env.runtime.script(Role.MERGER, merger({"impl.py": RESOLVED_IMPL}))
    story = run(env)
    assert story.status == StoryStatus.DONE
    (request,) = env.runtime.requests_for(Role.MERGER)
    assert "impl.py" in request.prompt and "value() is 2" in request.prompt
    assert (env.root / "impl.py").read_text() == RESOLVED_IMPL
    assert (env.root / "main_note.txt").read_text() == "from main"
    assert (env.root / "tests" / "check.py").read_text() == CHECK
    assert subjects(env.root)[:3] == ["feat(T1): Say two", "test(T1): Say two", "main moves on"]
    assert branch_names(env.root) == ["main"]
    assert not env.repo.rebase_in_progress()


def test_unresolved_conflict_retries_then_aborts_rebase_and_blocks(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    conflicting_main(env, {"impl.py": MAIN_IMPL})
    main_head = env.repo.head()
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    env.runtime.script(Role.MERGER, merger(), merger())
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason == "Merge conflict could not be resolved: impl.py"
    assert len(env.runtime.requests_for(Role.MERGER)) == 2
    assert "impl.py" in env.runtime.requests_for(Role.MERGER)[1].prompt
    assert "previous attempt" in env.runtime.requests_for(Role.MERGER)[1].prompt
    assert not env.repo.rebase_in_progress()
    env.repo.checkout("main")
    assert env.repo.head() == main_head
    assert not env.repo.has_changes()


def test_improvements_are_not_created_when_merge_blocks(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=1)
    conflicting_main(env, {"impl.py": MAIN_IMPL})
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok([Improvement(title="Cache it", description="Cache")]))
    env.runtime.script(Role.MERGER, merger())
    assert run(env).status == StoryStatus.BLOCKED
    assert not env.store.has("S-0002")


def test_merger_agent_error_retries_with_feedback(tmp_path: Path) -> None:
    env = make_env(tmp_path, max_attempts=2)
    conflicting_main(env, {"impl.py": MAIN_IMPL})
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    env.runtime.script(
        Role.MERGER, Step(agent_error("merger down")), merger({"impl.py": RESOLVED_IMPL})
    )
    assert run(env).status == StoryStatus.DONE
    assert "merger down" in env.runtime.requests_for(Role.MERGER)[1].prompt


def test_gate_failure_after_rebase_feeds_implementer(tmp_path: Path) -> None:
    lint = "test ! -f poison.txt || (echo poisoned-by-main; exit 1)"
    env = make_env(tmp_path, lint=lint)
    conflicting_main(env, {"poison.txt": "x"})
    env.runtime.script(Role.TEST_WRITER, tw())

    def unpoison(workdir: Path) -> None:
        (workdir / "poison.txt").unlink()

    env.runtime.script(Role.IMPLEMENTER, impl(), Step(ImplementerOutput(summary="fixed"), unpoison))
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    fix_prompt = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "poisoned-by-main" in fix_prompt
    assert subjects(env.root)[0] == "fix(merge): make gates pass after rebase"
    assert not (env.root / "poison.txt").exists()


def add_remote(env: Env, tmp_path: Path, hook: str | None = None) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    git(env.root, "remote", "add", "origin", str(bare))
    env.repo.push("origin", "main")
    if hook is not None:
        hook_path = bare / "hooks" / "pre-receive"
        hook_path.write_text(hook)
        hook_path.chmod(0o755)
    return bare


def test_pull_and_push_with_remote(tmp_path: Path) -> None:
    env = make_env(tmp_path, remote="origin")
    bare = add_remote(env, tmp_path)
    clone_path = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone_path)], check=True, capture_output=True)
    git(clone_path, "config", "user.name", "Other")
    git(clone_path, "config", "user.email", "other@example.com")
    git(clone_path, "config", "commit.gpgsign", "false")
    (clone_path / "other.txt").write_text("other")
    clone = GitRepo(clone_path)
    clone.commit_all("remote work")
    clone.push("origin", "main")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    assert run(env).status == StoryStatus.DONE
    assert (env.root / "other.txt").exists()
    remote_log = subjects(bare, "main")
    assert remote_log == ["feat(T1): Say two", "test(T1): Say two", "remote work"]
    assert branch_names(env.root) == ["main"]


def test_push_failure_blocks_story_with_merged_main(tmp_path: Path) -> None:
    env = make_env(tmp_path, remote="origin")
    bare = add_remote(env, tmp_path, hook="#!/bin/sh\necho rejected-by-hook >&2\nexit 1\n")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None
    assert story.blocked_reason.startswith("Merged locally but push failed:")
    assert "autodev resume" in story.blocked_reason
    assert subjects(env.root, "main") == ["feat(T1): Say two", "test(T1): Say two"]
    assert subjects(bare, "main") == []


def test_pull_failure_blocks_story(tmp_path: Path) -> None:
    env = make_env(tmp_path, status=StoryStatus.IN_QA, tasks=done_tasks(), remote="origin")
    git(env.root, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None and story.blocked_reason.startswith("Pull failed:")


def log_records(root: Path) -> list[dict[str, object]]:
    (log_file,) = (root / ".agent" / "runs" / "S-0001").glob("*.jsonl")
    return [json.loads(line) for line in log_file.read_text().splitlines()]


def show(root: Path, rev: str, name: str) -> str:
    return subprocess.run(
        ["git", "show", f"{rev}:{name}"], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def test_formatter_runs_before_each_agent_commit(tmp_path: Path) -> None:
    fmt = write_formatter(tmp_path)
    env = make_env(tmp_path, format=fmt)
    env.runtime.script(Role.TEST_WRITER, tw({"tests/check.py": CHECK + "# UGLY\n"}))
    env.runtime.script(Role.IMPLEMENTER, impl({"impl.py": IMPL_GOOD + "# UGLY\n"}))
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert show(env.root, "HEAD~1", "tests/check.py") == CHECK + "# PRETTY\n"
    assert show(env.root, "HEAD", "impl.py") == IMPL_GOOD + "# PRETTY\n"
    formats = [r for r in log_records(env.root) if r["event"] == "format"]
    assert len(formats) == 2
    assert all(r["ok"] is True for r in formats)


def test_red_restores_non_test_files_changed_only_by_formatter(tmp_path: Path) -> None:
    fmt = write_formatter(tmp_path)
    env = make_env(tmp_path, format=fmt, files={"legacy.py": "x = 1  # UGLY\n"})
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert len(env.runtime.requests_for(Role.TEST_WRITER)) == 1
    assert env.repo.changed_files("HEAD~1") == ["tests/check.py"]


TEST_LINT = "grep -Hn LINTBAD tests/check.py && exit 1 || exit 0"
BAD_LINT_TESTS = CHECK + "# LINTBAD\n"


def fix_tests(files: dict[str, str]) -> Step:
    return Step(TestWriterOutput(summary="fixed", test_files=["tests/check.py"]), writer(files))


def test_lint_failure_only_in_test_file_gets_test_writer_fix_turn(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint=TEST_LINT)
    env.runtime.script(
        Role.TEST_WRITER,
        tw({"tests/check.py": BAD_LINT_TESTS}),
        fix_tests({"tests/check.py": CHECK}),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 1
    fix_request = env.runtime.requests_for(Role.TEST_WRITER)[1]
    assert "tests/check.py:" in fix_request.prompt
    assert "only test files" in fix_request.prompt
    assert subjects(env.root) == [
        "test(T1): fix test lint",
        "feat(T1): Say two",
        "test(T1): Say two",
    ]
    events = [r["event"] for r in log_records(env.root)]
    assert "test_fix" in events


def test_test_fix_turn_touching_non_test_file_is_reset(tmp_path: Path) -> None:
    env = make_env(tmp_path, lint=TEST_LINT)
    env.runtime.script(
        Role.TEST_WRITER,
        tw({"tests/check.py": BAD_LINT_TESTS}),
        fix_tests({"tests/check.py": CHECK, "impl.py": IMPL_GOOD + "# touched\n"}),
        fix_tests({"tests/check.py": CHECK}),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    third = env.runtime.requests_for(Role.TEST_WRITER)[2]
    assert "only touch test files" in third.prompt and "impl.py" in third.prompt
    assert "touched" not in (env.root / "impl.py").read_text()
    assert subjects(env.root)[0] == "test(T1): fix test lint"


def test_resume_restarts_pending_task_from_scratch(tmp_path: Path) -> None:
    tasks = [Task("T1", "First", TaskStatus.DONE), Task("T2", "Second")]
    env = make_env(tmp_path, status=StoryStatus.IN_PROGRESS, tasks=tasks)
    env.repo.create_branch("story/S-0001", "main")
    writer({"tests/old_t1.py": "pass\n"})(env.root)
    env.repo.commit_all("test(T1): First")
    writer({"notes_t1.txt": "t1"})(env.root)
    kept = env.repo.commit_all("feat(T1): First")
    writer({"tests/old_t2.py": "pass\n"})(env.root)
    env.repo.commit_all("test(T2): Second")
    writer({"notes_t2.txt": "t2"})(env.root)
    env.repo.commit_all("feat(T2): Second")
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert subjects(env.root) == [
        "feat(T2): Second",
        "test(T2): Second",
        "feat(T1): First",
        "test(T1): First",
    ]
    assert not (env.root / "notes_t2.txt").exists()
    assert not (env.root / "tests" / "old_t2.py").exists()
    assert (env.root / "notes_t1.txt").exists()
    (restart,) = [r for r in log_records(env.root) if r["event"] == "task_restarted"]
    assert restart["task"] == "T2" and restart["detail"] == kept


def test_commit_subjects_are_short_and_keep_full_title_in_body(tmp_path: Path) -> None:
    title = (
        "Register the payment of an order when the customer confirms the cart contents "
        "and the stock reservation succeeds for every single line"
    )
    env = make_env(tmp_path, tasks=[Task("T1", title)])
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"impl.py": IMPL_GOOD + "# fix\n"}))
    env.runtime.script(Role.REVIEWER, reject(), approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=env.root, capture_output=True, text=True
    ).stdout.splitlines()[:-1]
    assert len(log) == 3
    for subject in log:
        assert len(subject) <= 72
        assert subject.startswith(("test(T1): ", "feat(T1): ", "fix(review): "))
    bodies = subprocess.run(
        ["git", "log", "--format=%b%x00"], cwd=env.root, capture_output=True, text=True
    ).stdout
    assert bodies.count(title) == 2


HELPER_BAD = "def expected():\n    return 3\n"
HELPER_GOOD = "def expected():\n    return 2\n"
CHECK_HELPER = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent))\n"
    "from helper import expected\nfrom impl import value\n"
    "def test_value():\n    pass\n"
    "def test_other():\n    pass\n"
    "sys.exit(0 if value() == expected() else 1)\n"
)
CHECK_HELPER_ONE_TEST = CHECK_HELPER.replace("def test_other():\n    pass\n", "")
DEFECT = "tests/helper.py expected() returns 3 instead of 2"


def defect_impl() -> Step:
    return Step(
        ImplementerOutput(summary="done", test_defect=DEFECT), writer({"impl.py": IMPL_GOOD})
    )


def defect_fix(files: dict[str, str]) -> Step:
    return Step(TestWriterOutput(summary="fixed", test_files=list(files)), writer(files))


def helper_red() -> Step:
    return tw({"tests/check.py": CHECK_HELPER, "tests/helper.py": HELPER_BAD})


def test_lint_test_fix_turn_triggers_despite_ignored_venv_path_in_output(tmp_path: Path) -> None:
    lint = (
        "grep -Hn LINTBAD tests/check.py && echo .venv/lib/dep.py:1 DeprecationWarning "
        "&& exit 1 || exit 0"
    )
    env = make_env(tmp_path, lint=lint)
    (env.root / ".venv/lib").mkdir(parents=True)
    (env.root / ".venv/lib/dep.py").write_text("x\n")
    (env.root / ".gitignore").write_text(".venv/\n")
    env.repo.commit_all("ignore venv")
    env.runtime.script(
        Role.TEST_WRITER,
        tw({"tests/check.py": BAD_LINT_TESTS}),
        fix_tests({"tests/check.py": CHECK}),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 1
    assert subjects(env.root)[0] == "test(T1): fix test lint"


def test_implementer_reported_test_defect_gets_test_writer_fix(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, helper_red(), defect_fix({"tests/helper.py": HELPER_GOOD}))
    env.runtime.script(Role.IMPLEMENTER, defect_impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 1
    fix_request = env.runtime.requests_for(Role.TEST_WRITER)[1]
    assert DEFECT in fix_request.prompt
    assert subjects(env.root)[:3] == [
        "test(T1): fix test defect",
        "feat(T1): Say two",
        "test(T1): Say two",
    ]
    events = [r for r in log_records(env.root) if r["event"] == "test_defect_fix"]
    assert events and events[-1]["ok"] is True


def test_test_defect_fix_that_removes_tests_is_reset(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(
        Role.TEST_WRITER,
        helper_red(),
        defect_fix({"tests/helper.py": HELPER_GOOD, "tests/check.py": CHECK_HELPER_ONE_TEST}),
        defect_fix({"tests/helper.py": HELPER_GOOD}),
    )
    env.runtime.script(Role.IMPLEMENTER, defect_impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    third = env.runtime.requests_for(Role.TEST_WRITER)[2]
    assert "2" in third.prompt and "1" in third.prompt and "test definitions" in third.prompt
    assert "def test_other" in (env.root / "tests/check.py").read_text()
    assert subjects(env.root)[0] == "test(T1): fix test defect"


def test_test_defect_fix_touching_production_code_is_reset(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(
        Role.TEST_WRITER,
        helper_red(),
        defect_fix({"tests/helper.py": HELPER_GOOD, "impl.py": IMPL_GOOD + "# touched\n"}),
        defect_fix({"tests/helper.py": HELPER_GOOD}),
    )
    env.runtime.script(Role.IMPLEMENTER, defect_impl())
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    third = env.runtime.requests_for(Role.TEST_WRITER)[2]
    assert "only touch test files" in third.prompt and "impl.py" in third.prompt
    assert "touched" not in (env.root / "impl.py").read_text()


def test_failure_without_test_defect_retries_implementer(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, helper_red())
    env.runtime.script(
        Role.IMPLEMENTER,
        impl({"impl.py": IMPL_GOOD}),
        impl({"impl.py": IMPL_GOOD + "# again\n"}),
        impl({"impl.py": IMPL_GOOD + "# and again\n"}),
    )
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert len(env.runtime.requests_for(Role.TEST_WRITER)) == 1
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 3


# -- per-test red (JUnit report configured) ----------------------------------

VALUE_TEST = "from impl import value\n\n\ndef test_value_is_two():\n    assert value() == 2\n"
VACUOUS_TEST = "def test_nothing():\n    assert True\n"
NEW_TEST = "from newmod import compute\n\n\ndef test_compute_is_two():\n    assert compute() == 2\n"
NEW_SCAFFOLD = "def compute() -> int:\n    raise NotImplementedError\n"
NEW_GOOD = "def compute() -> int:\n    return 2\n"


def jtw(files: dict[str, str], coverage: list[CriterionCoverage] | None = None) -> Step:
    """A test writer step; by default every test function maps to criterion 1."""
    if coverage is None:
        names = [n for content in files.values() for n in re.findall(r"def (test_\w+)", content)]
        coverage = [CriterionCoverage(criterion=1, tests=names)]
    out = TestWriterOutput(summary="tests", test_files=list(files), coverage=coverage)
    return Step(out, writer(files))


def scaffold(files: dict[str, str]) -> Step:
    return Step(ScaffoldOutput(summary="scaffold"), writer(files))


def finish_steps(env: Env) -> None:
    env.runtime.script(Role.REVIEWER, approve())
    env.runtime.script(Role.QA, qa_ok())


def events(env: Env, name: str) -> list[dict[str, object]]:
    return [r for r in log_records(env.root) if r["event"] == name]


def test_red_with_import_error_gets_scaffold_then_tests_fail_and_task_completes(
    tmp_path: Path,
) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_new.py": NEW_TEST}))
    env.runtime.script(Role.IMPLEMENTER, scaffold({"newmod.py": NEW_SCAFFOLD}))
    env.runtime.script(Role.IMPLEMENTER, impl({"newmod.py": NEW_GOOD}))
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert subjects(env.root) == [
        "feat(T1): Say two",
        "chore(T1): scaffold",
        "test(T1): Say two",
    ]
    scaffold_prompt = env.runtime.requests_for(Role.IMPLEMENTER)[0].prompt
    assert "Scaffold turn" in scaffold_prompt and "newmod" in scaffold_prompt
    assert len(events(env, "scaffold")) == 1
    (by_test,) = events(env, "red_by_test")
    assert by_test["ok"] is True and "test_compute_is_two" in str(by_test["detail"])


def test_vacuous_new_test_passing_at_red_is_reset_and_test_writer_retries(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(
        Role.TEST_WRITER,
        jtw({"tests/test_nothing.py": VACUOUS_TEST}),
        jtw({"tests/test_value.py": VALUE_TEST}),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    second = env.runtime.requests_for(Role.TEST_WRITER)[1].prompt
    assert "test_nothing" in second and "no behaviour" in second
    assert not (env.root / "tests" / "test_nothing.py").exists()
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    assert events(env, "red_vacuous")


def test_scaffold_touching_test_files_is_reset(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_new.py": NEW_TEST}))
    env.runtime.script(
        Role.IMPLEMENTER,
        scaffold({"newmod.py": NEW_SCAFFOLD, "tests/test_new.py": NEW_TEST + "# edited\n"}),
        scaffold({"newmod.py": NEW_SCAFFOLD}),
        impl({"newmod.py": NEW_GOOD}),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    retry = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "test files" in retry and "tests/test_new.py" in retry
    assert (env.root / "tests" / "test_new.py").read_text() == NEW_TEST
    assert subjects(env.root).count("chore(T1): scaffold") == 1
    assert len(events(env, "scaffold")) == 2


def test_new_test_error_after_scaffold_resets_to_pre_red(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True, max_attempts=4)
    wants_two = "from newmod import compute, other\n\n\ndef test_both():\n    assert other()\n"
    env.runtime.script(
        Role.TEST_WRITER,
        jtw({"tests/test_new.py": wants_two}),
        jtw({"tests/test_new.py": NEW_TEST}),
    )
    env.runtime.script(
        Role.IMPLEMENTER,
        scaffold({"newmod.py": NEW_SCAFFOLD}),
        scaffold({"newmod.py": NEW_SCAFFOLD}),
        impl({"newmod.py": NEW_GOOD}),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    second = env.runtime.requests_for(Role.TEST_WRITER)[1].prompt
    assert "tests.test_new" in second and "other" in second
    assert subjects(env.root) == [
        "feat(T1): Say two",
        "chore(T1): scaffold",
        "test(T1): Say two",
    ]


def test_green_requires_every_new_test_to_pass_by_id(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    source = (
        "import unittest\nfrom impl import value\n\n\n"
        "def test_picky():\n"
        "    assert value() != 1\n"
        "    if value() == 2:\n"
        "        raise unittest.SkipTest('skipped by the implementation')\n"
    )
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_picky.py": source}))
    env.runtime.script(
        Role.IMPLEMENTER,
        impl({"impl.py": IMPL_GOOD}),
        impl({"impl.py": "def value():\n    return 3\n"}),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    retry = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "test_picky" in retry and "skipped" in retry and "did not pass" in retry


def test_final_recheck_catches_test_weakened_by_test_fix_turn(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True, max_attempts=2)
    weak = {"tests/test_value.py": "def test_value_is_two():\n    assert True\n"}
    good = {"tests/test_value.py": VALUE_TEST}
    declare = Step(ImplementerOutput(summary="no change", test_defect=DEFECT))
    env.runtime.script(Role.TEST_WRITER, jtw(good), jtw(weak), jtw(good), jtw(weak))
    env.runtime.script(Role.IMPLEMENTER, declare, declare)
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.blocked_reason is not None
    assert "without the implementation" in story.blocked_reason
    assert story.tasks[0].status == TaskStatus.PENDING
    failed = events(env, "red_recheck_failed")
    assert len(failed) == 2 and "test_value_is_two" in str(failed[0]["detail"])
    restart = env.runtime.requests_for(Role.TEST_WRITER)[2].prompt
    assert "test_value_is_two" in restart and "depend on the implementation" in restart
    assert subjects(env.root) == []
    assert not env.repo.has_changes()


def test_final_recheck_restores_working_tree_and_keeps_commits(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_value.py": VALUE_TEST}))
    env.runtime.script(Role.IMPLEMENTER, impl({"impl.py": IMPL_GOOD, "extra.py": "X = 1\n"}))
    env.runtime.script(Role.REVIEWER, Step(agent_error("stop here")))
    story = run(env)
    assert story.status == StoryStatus.BLOCKED
    assert story.tasks[0].status == TaskStatus.DONE
    assert env.repo.current_branch() == "story/S-0001"
    assert not env.repo.has_changes()
    assert (env.root / "impl.py").read_text() == IMPL_GOOD
    assert (env.root / "extra.py").read_text() == "X = 1\n"
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    (recheck,) = events(env, "red_recheck")
    assert recheck["ok"] is True


def test_second_task_ignores_tests_of_the_first_task(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True, tasks=[Task("T1", "Say two"), Task("T2", "Say three")])
    other_test = "from impl import other\n\n\ndef test_other_is_three():\n    assert other() == 3\n"
    env.runtime.script(
        Role.TEST_WRITER,
        jtw({"tests/test_value.py": VALUE_TEST}),
        jtw({"tests/test_other.py": other_test}),
    )
    env.runtime.script(
        Role.IMPLEMENTER,
        impl(),
        scaffold(
            {"impl.py": IMPL_GOOD + "\n\ndef other() -> int:\n    raise NotImplementedError\n"}
        ),
        impl({"impl.py": IMPL_GOOD + "\n\ndef other() -> int:\n    return 3\n"}),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert subjects(env.root) == [
        "feat(T2): Say three",
        "chore(T2): scaffold",
        "test(T2): Say three",
        "feat(T1): Say two",
        "test(T1): Say two",
    ]


def test_without_junit_report_behaviour_is_unchanged(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(Role.IMPLEMENTER, impl())
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    assert len(events(env, "junit_report_missing")) == 1
    for name in ("scaffold", "red_by_test", "red_recheck", "red_vacuous"):
        assert events(env, name) == []


# -- traceability, test changes after red, placeholders ------------------------


def test_red_coverage_with_unknown_test_name_is_rejected(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    files = {"tests/test_value.py": VALUE_TEST}
    unknown = [CriterionCoverage(criterion=1, tests=["test_does_not_exist"])]
    out_of_range = [CriterionCoverage(criterion=5, tests=["test_value_is_two"])]
    env.runtime.script(
        Role.TEST_WRITER,
        jtw(files, unknown),
        jtw(files, out_of_range),
        jtw(files),
    )
    env.runtime.script(Role.IMPLEMENTER, impl())
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    prompts = [r.prompt for r in env.runtime.requests_for(Role.TEST_WRITER)]
    assert "test_does_not_exist" in prompts[1]
    assert "criterion 5" in prompts[2]
    assert subjects(env.root) == ["feat(T1): Say two", "test(T1): Say two"]
    assert [e["ok"] for e in events(env, "red_coverage")] == [False, False, True]


def test_task_coverage_is_persisted(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_value.py": VALUE_TEST}))
    env.runtime.script(Role.IMPLEMENTER, impl())
    finish_steps(env)
    run(env)
    assert env.store.get("S-0001").tasks[0].coverage == {1: ["test_value_is_two"]}


WRONG_TEST = "from impl import value\n\n\ndef test_value_is_two():\n    assert value() == 3\n"


def defect_in_test_impl() -> Step:
    out = ImplementerOutput(summary="done", test_defect="test_value_is_two expects 3, not 2")
    return Step(out, writer({"impl.py": IMPL_GOOD}))


def confirming_review() -> Step:
    out = ReviewOutput(approved=True, summary="ok", findings=[], test_changes_confirmed=True)
    return Step(out)


def test_review_approval_overridden_when_test_changes_not_confirmed(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(
        Role.TEST_WRITER,
        jtw({"tests/test_value.py": WRONG_TEST}),
        jtw({"tests/test_value.py": VALUE_TEST}),
    )
    unconfirmed = Step(ReviewOutput(approved=True, summary="lgtm", findings=[]))
    env.runtime.script(Role.IMPLEMENTER, defect_in_test_impl(), impl({"extra.py": "X = 1\n"}))
    env.runtime.script(Role.REVIEWER, unconfirmed, confirming_review())
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    first, second = (r.prompt for r in env.runtime.requests_for(Role.REVIEWER))
    assert "-    assert value() == 3" in first and "+    assert value() == 2" in first
    assert "tests/test_value.py" in first
    fix = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "Confirm the test changes after red against the acceptance criteria" in fix
    (overridden,) = events(env, "review_overridden")
    assert "test changes" in str(overridden["detail"])
    assert second  # reviewed twice


def two_criteria(env: Env) -> None:
    story = env.store.get("S-0001")
    story.acceptance_criteria = ["value() is 2", "value() is not 3"]
    env.store.save(story)


def test_review_approval_overridden_for_uncovered_criterion_without_reason(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    two_criteria(env)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_value.py": VALUE_TEST}))
    env.runtime.script(Role.IMPLEMENTER, impl(), impl({"extra.py": "X = 1\n"}))
    blank = UncoveredCriterion(criterion=2, reason="  ")
    first = Step(
        ReviewOutput(
            approved=True,
            summary="lgtm",
            findings=[],
            test_changes_confirmed=True,
            uncovered_criteria=[blank],
        )
    )
    second = Step(
        ReviewOutput(
            approved=True,
            summary="lgtm",
            findings=[],
            test_changes_confirmed=True,
            uncovered_criteria=[UncoveredCriterion(criterion=2, reason="negative, not testable")],
        )
    )
    env.runtime.script(Role.REVIEWER, first, second)
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    prompt = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "criterion 2" in prompt and "value() is not 3" in prompt
    assert len(events(env, "review_overridden")) == 1


def test_uncovered_criterion_with_reason_is_accepted(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    two_criteria(env)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_value.py": VALUE_TEST}))
    env.runtime.script(Role.IMPLEMENTER, impl())
    review = ReviewOutput(
        approved=True,
        summary="lgtm",
        findings=[],
        test_changes_confirmed=True,
        uncovered_criteria=[UncoveredCriterion(criterion=2, reason="not testable")],
    )
    env.runtime.script(Role.REVIEWER, Step(review))
    env.runtime.script(Role.QA, qa_ok())
    story = run(env)
    assert story.status == StoryStatus.DONE
    assert events(env, "review_overridden") == []
    prompt = env.runtime.requests_for(Role.REVIEWER)[0].prompt
    assert "Criteria without tests: 2" in prompt


def test_forbidden_pattern_added_in_green_fails_attempt_with_feedback(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.TEST_WRITER, tw())
    env.runtime.script(
        Role.IMPLEMENTER,
        impl({"impl.py": "def value():\n    return 2  # TODO real logic\n"}),
        impl(),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    retry = env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt
    assert "impl.py:2:" in retry and "TODO" in retry
    assert subjects(env.root) == [
        "fix(T1): Say two",
        "feat(T1): Say two",
        "test(T1): Say two",
    ]
    (found,) = events(env, "forbidden_patterns")
    assert found["ok"] is False and "impl.py:2:" in str(found["detail"])


def test_scaffold_not_implemented_left_after_green_is_caught(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True)
    env.runtime.script(Role.TEST_WRITER, jtw({"tests/test_new.py": NEW_TEST}))
    left_over = NEW_GOOD + "\n\ndef other() -> int:\n    raise NotImplementedError\n"
    env.runtime.script(
        Role.IMPLEMENTER,
        scaffold(
            {"newmod.py": NEW_SCAFFOLD + "\n\ndef other() -> int:\n    raise NotImplementedError\n"}
        ),
        impl({"newmod.py": left_over}),
        impl({"newmod.py": NEW_GOOD}),
    )
    finish_steps(env)
    story = run(env)
    assert story.status == StoryStatus.DONE
    retry = env.runtime.requests_for(Role.IMPLEMENTER)[2].prompt
    assert "newmod.py" in retry and "NotImplementedError" in retry
    assert "NotImplementedError" not in (env.root / "newmod.py").read_text()


def test_run_rejects_junit_report_path_not_ignored(tmp_path: Path) -> None:
    env = make_env(tmp_path, junit=True, junit_path="reports/junit.xml")
    with pytest.raises(RunError, match=r"tdd\.junit_report must point to a git-ignored path"):
        run(env)
