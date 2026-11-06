import asyncio
import shutil
import subprocess
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.adapters.knowledge_folder import KnowledgeFolder
from autodev.config import load_config
from autodev.domain.role import Role
from autodev.improve.process_retro import NoRunsYet, run_process_retro
from autodev.pipeline.outputs import ProcessProposal, ProcessReview
from autodev.pipeline.prompts import load_instructions
from autodev.pipeline.run_log import RunLog
from autodev.pipeline.state_store import StoryStore
from autodev.pipeline.story_runner import RunError
from autodev.ports.agent_runtime import AgentRuntime
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error
from tests.integration.conftest import git


@dataclass
class Env:
    root: Path
    repo: GitRepo
    runtime: FakeAgentRuntime


def make_env(tmp_path: Path, *, with_runs: bool = True) -> Env:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    with as_file(files("autodev") / "templates" / "agent") as template:
        shutil.copytree(template, root / ".agent")
    (root / ".agent" / "standards").mkdir(exist_ok=True)
    (root / ".agent" / "standards" / "testing.md").write_text("# Testing\n- one\n")
    repo.commit_all("initial")
    if with_runs:
        log = RunLog(root, "S-0001")
        log.append("run_started", detail="approved")
        log.append("agent_run", role="implementer", ok=True, cost_usd=0.5)
        log.append("story_done")
    return Env(root, repo, FakeAgentRuntime())


def prompt_proposal(target: str = "implementer", content: str = "NEW RULES") -> ProcessProposal:
    return ProcessProposal(kind="prompt", target=target, rationale="why", content=content)


def standard_proposal(target: str = "testing.md", content: str = "- two") -> ProcessProposal:
    return ProcessProposal(kind="standard", target=target, rationale="why", content=content)


def config_proposal() -> ProcessProposal:
    return ProcessProposal(
        kind="config", target="limits.max_attempts", rationale="why", content="raise to 5"
    )


def retro(env: Env, io: FakeIO, runtime: AgentRuntime | None = None) -> list[ProcessProposal]:
    return asyncio.run(
        run_process_retro(
            config=load_config(env.root),
            project_root=env.root,
            runtime=runtime or env.runtime,
            repo=env.repo,
            context=KnowledgeFolder(env.root),
            store=StoryStore(env.root),
            io=io,
            log=RunLog(env.root, "improve"),
        )
    )


def script(env: Env, *proposals: ProcessProposal) -> None:
    env.runtime.script(
        Role.IMPROVER, Step(ProcessReview(summary="Retro summary", proposals=list(proposals)))
    )


def commit_messages(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_accepts_prompt_and_standard_and_commits(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    script(env, prompt_proposal(), standard_proposal(), config_proposal())
    io = FakeIO("a", "a", "a")
    accepted = retro(env, io)
    assert [p.kind for p in accepted] == ["prompt", "standard", "config"]
    assert (env.root / ".agent" / "process" / "implementer.md").read_text() == "NEW RULES"
    assert (env.root / ".agent" / "standards" / "testing.md").read_text() == (
        "# Testing\n- one\n\n- two\n"
    )
    assert commit_messages(env.root)[0] == "chore(process): apply retrospective proposals"
    assert not env.repo.has_changes()
    shown = "\n".join(io.shown)
    assert "Retro summary" in shown
    assert "Recommended config changes (apply manually)" in shown
    assert "raise to 5" in shown
    state = env.root / ".agent" / "runs" / "improve-state.json"
    assert state.is_file()
    assert env.runtime.requests_for(Role.IMPROVER)[0].output_model is ProcessReview
    assert "implementer" in env.runtime.requests_for(Role.IMPROVER)[0].prompt


def test_creates_missing_standards_file(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    script(env, standard_proposal("naming.md", "- be clear  \n"))
    retro(env, FakeIO("a"))
    assert (env.root / ".agent" / "standards" / "naming.md").read_text() == "\n- be clear\n"


def test_skipped_and_config_proposals_are_not_written(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    before = commit_messages(env.root)
    script(env, prompt_proposal(), standard_proposal(), config_proposal())
    io = FakeIO("x", "s", "s", "a")
    accepted = retro(env, io)
    assert [p.kind for p in accepted] == ["config"]
    assert not (env.root / ".agent" / "process" / "implementer.md").exists()
    assert (env.root / ".agent" / "standards" / "testing.md").read_text() == "# Testing\n- one\n"
    assert commit_messages(env.root) == before
    assert "raise to 5" in "\n".join(io.shown)
    assert io.prompts[0] == "[a]ccept, [s]kip"


def test_invalid_targets_are_skipped_with_warning(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    script(
        env,
        prompt_proposal("wizard"),
        standard_proposal("../evil.md"),
        standard_proposal("sub/dir.md"),
        standard_proposal("notes.txt"),
    )
    io = FakeIO("a", "a", "a", "a")
    accepted = retro(env, io)
    assert accepted == []
    assert not (env.root / ".agent" / "process" / "wizard.md").exists()
    assert not (env.root / ".agent" / "evil.md").exists()
    assert not env.repo.has_changes()
    warnings = [s for s in io.shown if s.lower().startswith("warning")]
    assert len(warnings) == 4


def test_no_runs_raises(tmp_path: Path) -> None:
    env = make_env(tmp_path, with_runs=False)
    with pytest.raises(NoRunsYet, match="No runs yet; nothing to learn from"):
        retro(env, FakeIO())
    assert env.runtime.requests == []


def test_requires_main_and_clean_tree(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    (env.root / "dirty.txt").write_text("x")
    with pytest.raises(RunError, match="clean"):
        retro(env, FakeIO())
    env.repo.commit_all("clean again")
    env.repo.create_branch("story/S-1", "main")
    with pytest.raises(RunError, match="main"):
        retro(env, FakeIO())
    env.repo.checkout("main")
    (env.root / ".agent" / ".gitignore").unlink()
    env.repo.commit_all("no ignore")
    with pytest.raises(RunError, match="not ignored"):
        retro(env, FakeIO())
    assert env.runtime.requests == []


def test_agent_error_becomes_run_error(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.runtime.script(Role.IMPROVER, Step(agent_error("boom")))
    with pytest.raises(RunError, match="boom"):
        retro(env, FakeIO())


def test_written_prompt_override_is_used_by_load_instructions(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    script(env, prompt_proposal("reviewer", "REVIEW RULES"))
    retro(env, FakeIO("a"))
    config = load_config(env.root)
    profile = config.agents.profile_for(Role.REVIEWER)
    assert load_instructions(Role.REVIEWER, profile, env.root / ".agent") == "REVIEW RULES"
