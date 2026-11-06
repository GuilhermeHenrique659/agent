import asyncio
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.config import ConfigError, load_config
from autodev.domain.role import Role
from autodev.onboarding.doctor import ConnectionAnswer
from autodev.onboarding.onboard import OnboardError, OnboardReport, confirm, onboard
from autodev.pipeline.outputs import (
    AdrDraft,
    BootstrapOutput,
    Commands,
    TechBrief,
    TechBriefTurn,
)
from tests.fakes import FakeAgentRuntime, FakeIO, Step, agent_error
from tests.integration.conftest import git

PY = shlex.quote(sys.executable)
TEST_CMD = f"{PY} -B smoke.py"


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{var}_NAME", "Test")
        monkeypatch.setenv(f"{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")


@dataclass
class Env:
    root: Path
    home: Path
    runtime: FakeAgentRuntime

    def run(self, io: FakeIO, *, yes: bool = False, brief: bool = True) -> OnboardReport:
        return asyncio.run(
            onboard(
                self.root,
                io=io,
                make_runtime=lambda config, root: self.runtime,
                yes=yes,
                run_brief_step=brief,
                home=self.home,
            )
        )

    def log(self) -> list[str]:
        out = subprocess.run(
            ["git", "log", "--format=%s"], cwd=self.root, capture_output=True, text=True
        )
        return out.stdout.split("\n")[:-1] if out.returncode == 0 else []


@pytest.fixture
def env(tmp_path: Path) -> Env:
    home = tmp_path / "home"
    home.mkdir()
    return Env(tmp_path / "proj", home, FakeAgentRuntime())


def write_auth_home(home: Path) -> None:
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"apiKeyHelper": "/bin/h", "env": {"ANTHROPIC_BASE_URL": "https://x"}})
    )


def ok_doctor(env: Env) -> None:
    env.runtime.script(Role.IMPLEMENTER, Step(ConnectionAnswer(answer=5)))


def brief_step(test: str = TEST_CMD) -> Step:
    brief = TechBrief(
        summary="A Python CLI",
        adrs=[AdrDraft(title="Use Python", context="c", decision="Python 3.12", consequences="q")],
        commands=Commands(test=test),
    )
    return Step(TechBriefTurn(message="Proposal", done=True, result=brief))


def smoke(workdir: Path) -> None:
    (workdir / "smoke.py").write_text("import sys\nsys.exit(0)\n")


def make_repo(root: Path, files: dict[str, str]) -> GitRepo:
    root.mkdir(parents=True)
    repo = GitRepo(root)
    repo.init("main")
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    repo.commit_all("initial")
    return repo


def prompts_containing(io: FakeIO, text: str) -> list[str]:
    return [p for p in io.prompts if text in p]


def test_confirm_answers_and_yes() -> None:
    io = FakeIO("", "N", "maybe", "YES")
    assert confirm(io, "q", True, False) is True
    assert confirm(io, "q", True, False) is False
    assert confirm(io, "q", False, False) is True  # "maybe" re-asked, then YES
    assert len(io.prompts) == 4
    assert confirm(FakeIO(), "q", False, True) is False


def test_new_empty_directory_full_onboard_with_brief_and_bootstrap(env: Env) -> None:
    write_auth_home(env.home)
    ok_doctor(env)
    env.runtime.script(Role.ARCHITECT, brief_step())
    env.runtime.script(Role.IMPLEMENTER, Step(BootstrapOutput(summary="skeleton"), smoke))
    # create dir, budget default, use auth, brief, accept the proposal
    io = FakeIO("", "", "", "", "a")
    report = env.run(io)
    assert (env.root / ".git").is_dir()
    assert (env.root / "smoke.py").is_file()
    assert env.log() == [
        "chore(bootstrap): project skeleton",
        "docs: technical briefing",
        "chore: onboard autodev",
    ]
    config = load_config(env.root)
    assert config.runtime.settings == "auth.json"
    assert config.agents.defaults.max_budget_usd == 5.0
    assert (env.root / ".agent" / "auth.json").is_file()
    assert report.next_steps[-1] == 'autodev request "<what you want>"'
    assert any("Create" in a or "Created" in a for a in report.actions)
    assert len(env.runtime.requests_for(Role.ARCHITECT)) == 1


def test_existing_repo_with_code_onboard_without_bootstrap(env: Env) -> None:
    make_repo(env.root, {"app.py": "print('hi')\n"})
    ok_doctor(env)
    env.runtime.script(Role.ARCHITECT, brief_step())
    io = FakeIO("", "", "a")  # budget, brief, accept
    env.run(io)
    assert env.log() == ["docs: technical briefing", "chore: onboard autodev", "initial"]
    assert env.runtime.requests_for(Role.IMPLEMENTER)[0].output_model is ConnectionAnswer
    assert len(env.runtime.requests_for(Role.IMPLEMENTER)) == 1  # doctor only, no bootstrap
    assert not (env.root / "smoke.py").exists()


def test_yes_accepts_defaults_without_asking(env: Env) -> None:
    make_repo(env.root, {"README.md": "hi\n"})
    write_auth_home(env.home)
    ok_doctor(env)
    env.runtime.script(Role.ARCHITECT, brief_step())
    io = FakeIO("a")
    env.run(io, yes=True)
    assert io.prompts == ["[a]ccept, [f]eedback"]  # only the briefing chat itself asks
    config = load_config(env.root)
    assert config.runtime.settings == "auth.json"
    assert config.context.paths == ["README.md"]
    assert config.agents.defaults.max_budget_usd == 5.0
    assert config.git.remote is None


def test_rerun_is_idempotent(env: Env) -> None:
    make_repo(env.root, {"README.md": "hi\n", "AGENTS.md": "rules\n"})
    write_auth_home(env.home)
    ok_doctor(env)
    env.runtime.script(Role.ARCHITECT, brief_step())
    env.run(FakeIO("a"), yes=True)
    commits = env.log()
    ok_doctor(env)
    io = FakeIO()
    report = env.run(io)
    assert io.prompts == []
    assert env.log() == commits
    assert any("already onboarded" in a for a in report.actions)
    assert any("nothing to commit" in a.lower() for a in report.actions)


def test_auth_minimal_settings_written_and_configured(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    write_auth_home(env.home)
    ok_doctor(env)
    env.run(FakeIO("", "", "n"))
    auth = json.loads((env.root / ".agent" / "auth.json").read_text())
    assert auth == {"apiKeyHelper": "/bin/h", "env": {"ANTHROPIC_BASE_URL": "https://x"}}
    assert load_config(env.root).runtime.settings == "auth.json"
    ignore = (env.root / ".agent" / ".gitignore").read_text().split()
    assert {"backlog/", "runs/", "auth.json"} <= set(ignore)
    tracked = GitRepo(env.root).tracked_files()
    assert ".agent/auth.json" not in tracked


def test_no_auth_settings_adds_next_step(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    ok_doctor(env)
    report = env.run(FakeIO("", "n"))
    assert load_config(env.root).runtime.settings is None
    assert any("Configure authentication" in s for s in report.next_steps)


def test_remote_default_is_not_allowed(env: Env, tmp_path: Path) -> None:
    repo = make_repo(env.root, {"app.py": "x\n"})
    git(env.root, "remote", "add", "origin", str(tmp_path))
    assert repo.remotes() == ["origin"]
    ok_doctor(env)
    io = FakeIO("", "", "n")  # remote default, budget, brief no
    env.run(io)
    assert load_config(env.root).git.remote is None
    assert prompts_containing(io, "origin")
    ok_doctor(env)
    env.run(FakeIO("y", "n"))
    assert load_config(env.root).git.remote == "origin"


def test_docs_added_to_context_paths(env: Env) -> None:
    make_repo(env.root, {"README.md": "r\n", "docs/a.md": "a\n", "other/x.md": "x\n"})
    ok_doctor(env)
    io = FakeIO("", "", "n")  # budget, docs, brief
    env.run(io)
    assert load_config(env.root).context.paths == ["README.md", "docs/"]
    assert prompts_containing(io, "README.md, docs/")


def test_agents_md_import_added_to_claude_md(env: Env) -> None:
    make_repo(env.root, {"AGENTS.md": "rules\n", "CLAUDE.md": "# Claude\n"})
    ok_doctor(env)
    env.run(FakeIO("", "", "n"))  # budget, add import, brief no
    assert (env.root / "CLAUDE.md").read_text() == "# Claude\n\n@AGENTS.md\n"
    assert env.log()[0] == "chore: onboard autodev"
    # already imported: not asked again
    ok_doctor(env)
    io = FakeIO("n")
    env.run(io)
    assert not prompts_containing(io, "@AGENTS.md")


def test_agents_md_without_claude_md_creates_it(env: Env) -> None:
    make_repo(env.root, {"AGENTS.md": "rules\n"})
    ok_doctor(env)
    env.run(FakeIO("", "", "n"))
    assert (env.root / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert "CLAUDE.md" in GitRepo(env.root).tracked_files()


def test_doctor_failure_stops_before_commit(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    env.runtime.script(Role.IMPLEMENTER, Step(agent_error("auth failed")))
    with pytest.raises(OnboardError, match="auth failed") as info:
        env.run(FakeIO("", ""))
    assert "runtime.settings" in str(info.value)
    assert env.log() == ["initial"]
    assert (env.root / ".agent" / "config.yaml").is_file()
    # rerunning is safe
    ok_doctor(env)
    env.run(FakeIO("n"))
    assert env.log()[0] == "chore: onboard autodev"


def test_brief_skipped_when_tree_has_other_changes(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    (env.root / "app.py").write_text("changed\n")
    ok_doctor(env)
    report = env.run(FakeIO("", ""))  # budget, brief yes
    assert env.runtime.requests_for(Role.ARCHITECT) == []
    assert any(
        "Commit or stash your changes, then run `autodev brief`" in s for s in report.next_steps
    )
    assert env.log()[0] == "chore: onboard autodev"


def test_no_brief_flag(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    ok_doctor(env)
    io = FakeIO("")
    report = env.run(io, brief=False)
    assert not prompts_containing(io, "briefing")
    assert env.runtime.requests_for(Role.ARCHITECT) == []
    assert "autodev brief" in report.next_steps
    assert report.next_steps[-1] == 'autodev request "<what you want>"'


def test_brief_failure_is_reported_not_raised(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    ok_doctor(env)
    env.runtime.script(Role.ARCHITECT, Step(agent_error("model down")))
    report = env.run(FakeIO("", ""))
    assert any("model down" in a for a in report.actions)
    assert "autodev brief" in report.next_steps


def test_declining_directory_creation_or_git_init_fails(env: Env) -> None:
    with pytest.raises(OnboardError, match="Nothing to do"):
        env.run(FakeIO("n"))
    assert not env.root.exists()
    env.root.mkdir()
    (env.root / "file.txt").write_text("x")
    with pytest.raises(OnboardError):
        env.run(FakeIO("n"))
    assert not (env.root / ".git").exists()


def test_invalid_config_raises_config_error(env: Env) -> None:
    make_repo(env.root, {"app.py": "x\n"})
    (env.root / ".agent").mkdir()
    (env.root / ".agent" / "config.yaml").write_text("bogus: 1\n")
    with pytest.raises(ConfigError):
        env.run(FakeIO())
