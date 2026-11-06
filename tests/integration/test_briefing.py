import asyncio
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config import load_config
from autodev.config.models import ProjectConfig
from autodev.domain.role import Role
from autodev.pipeline.briefing import run_brief
from autodev.pipeline.outputs import (
    AdrDraft,
    BootstrapOutput,
    Commands,
    TechBrief,
    TechBriefTurn,
)
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.story_runner import RunError
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error
from tests.integration.conftest import git

PY = shlex.quote(sys.executable)
TEST_CMD = f"{PY} -B smoke.py"


@dataclass
class Env:
    root: Path
    repo: GitRepo
    runtime: FakeAgentRuntime


def make_env(tmp_path: Path, *, existing_code: bool = False, gitignore: bool = True) -> Env:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    with as_file(files("autodev") / "templates" / "agent") as template:
        import shutil

        shutil.copytree(template, root / ".agent")
    if not gitignore:
        (root / ".agent" / ".gitignore").unlink()
    if existing_code:
        (root / "app.py").write_text("print('hi')\n")
    repo.commit_all("initial")
    return Env(root, repo, FakeAgentRuntime())


def brief(test: str | None = TEST_CMD, title: str = "Use Python") -> TechBrief:
    return TechBrief(
        summary="A Python CLI",
        adrs=[AdrDraft(title=title, context="c", decision="Python 3.12", consequences="q")],
        commands=Commands(build=None, test=test, lint=None),
        standards=["Type everything"],
    )


def brief_turn(b: TechBrief | None = None) -> Step:
    return Step(TechBriefTurn(message="Here is the proposal", done=True, result=b or brief()))


def write_smoke(ok: bool = True) -> Callable[[Path], None]:
    def edit(workdir: Path) -> None:
        (workdir / "smoke.py").write_text(
            "import sys\nsys.exit(0)\n" if ok else "raise SystemExit(1)\n"
        )

    return edit


def bootstrap_step(ok: bool = True) -> Step:
    return Step(BootstrapOutput(summary="skeleton"), write_smoke(ok))


def run(env: Env, io: FakeIO, config: ProjectConfig | None = None) -> TechBrief:
    return asyncio.run(
        run_brief(
            config=config or load_config(env.root),
            project_root=env.root,
            runtime=env.runtime,
            repo=env.repo,
            context=KnowledgeFolder(env.root),
            io=io,
            log=RunLog(env.root, "brief"),
        )
    )


def subjects(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


def test_brief_on_empty_repo_writes_docs_commits_and_bootstraps(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.ARCHITECT, brief_turn())
    env.runtime.script(Role.IMPLEMENTER, bootstrap_step())
    io = FakeIO("a")
    result = run(env, io)
    assert result.summary == "A Python CLI"
    assert subjects(env.root)[:2] == [
        "chore(bootstrap): project skeleton",
        "docs: technical briefing",
    ]
    adr = env.root / ".agent/architecture/adr/0001-use-python.md"
    assert "Python 3.12" in adr.read_text()
    assert "Accepted" in adr.read_text()
    stack = (env.root / ".agent/standards/stack.md").read_text()
    assert stack == "# Stack and standards\n\nA Python CLI\n\n- Type everything\n"
    assert load_config(env.root).commands.test == TEST_CMD
    assert (env.root / "smoke.py").is_file()
    assert any("Use Python" in text for text in io.shown)
    assert io.prompts == ["[a]ccept, [f]eedback"]
    prompt = env.runtime.requests_for(Role.IMPLEMENTER)[0].prompt
    assert "smoke test" in prompt
    assert not env.repo.has_changes()


def test_brief_on_existing_repo_skips_bootstrap(tmp_path: Path) -> None:
    env = make_env(tmp_path, existing_code=True)
    env.runtime.script(Role.ARCHITECT, brief_turn())
    run(env, FakeIO("a"))
    assert subjects(env.root)[0] == "docs: technical briefing"
    assert env.runtime.requests_for(Role.IMPLEMENTER) == []
    first = env.runtime.messages_for(Role.ARCHITECT)[0]
    assert "already has code" in first


def test_brief_feedback_reruns_architect_with_previous_proposal(tmp_path: Path) -> None:
    env = make_env(tmp_path, existing_code=True)
    env.runtime.script(Role.ARCHITECT, brief_turn(brief(title="Use Go")), brief_turn())
    io = FakeIO("x", "f", "prefer Python", "a")
    result = run(env, io)
    assert result.adrs[0].title == "Use Python"
    second = env.runtime.conversations[1].messages[0]
    assert "Use Go" in second and "prefer Python" in second
    assert io.prompts == [
        "[a]ccept, [f]eedback",
        "[a]ccept, [f]eedback",
        "Feedback",
        "[a]ccept, [f]eedback",
    ]
    adrs = sorted((env.root / ".agent/architecture/adr").glob("0001-*.md"))
    assert [p.name for p in adrs] == ["0001-use-python.md"]


def test_bootstrap_failure_resets_to_docs_commit_and_raises(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.ARCHITECT, brief_turn())
    env.runtime.script(
        Role.IMPLEMENTER,
        bootstrap_step(ok=False),
        Step(agent_error("boom")),
        bootstrap_step(ok=False),
    )
    with pytest.raises(RunError, match="Bootstrap failed"):
        run(env, FakeIO("a"))
    assert subjects(env.root)[0] == "docs: technical briefing"
    assert not (env.root / "smoke.py").exists()
    assert not env.repo.has_changes()
    prompts = [r.prompt for r in env.runtime.requests_for(Role.IMPLEMENTER)]
    assert len(prompts) == 3
    assert "Gate test failed" in prompts[1]
    assert "boom" in prompts[2]


def test_bootstrap_retries_on_top_of_failed_gates(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.ARCHITECT, brief_turn())
    env.runtime.script(Role.IMPLEMENTER, bootstrap_step(ok=False), bootstrap_step(ok=True))
    run(env, FakeIO("a"))
    assert subjects(env.root)[:3] == [
        "fix(bootstrap): project skeleton",
        "chore(bootstrap): project skeleton",
        "docs: technical briefing",
    ]
    assert "Gate test failed" in env.runtime.requests_for(Role.IMPLEMENTER)[1].prompt


def test_brief_requires_main_and_clean_tree(tmp_path: Path) -> None:
    env = make_env(tmp_path, existing_code=True)
    (env.root / "dirty.txt").write_text("x")
    with pytest.raises(RunError, match="uncommitted"):
        run(env, FakeIO())
    env.repo.commit_all("clean")
    env.repo.create_branch("feature", "main")
    with pytest.raises(RunError, match="main"):
        run(env, FakeIO())
    assert env.runtime.requests == []

    other = make_env(tmp_path / "o", existing_code=True, gitignore=False)
    with pytest.raises(RunError, match="not ignored"):
        run(other, FakeIO())


def test_brief_without_test_command_raises_before_bootstrap(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.ARCHITECT, brief_turn(brief(test=None)))
    with pytest.raises(RunError, match="no test command"):
        run(env, FakeIO("a"))
    assert env.runtime.requests_for(Role.IMPLEMENTER) == []
    assert subjects(env.root)[0] == "docs: technical briefing"
