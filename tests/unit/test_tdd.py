import shlex
import sys
from pathlib import Path

import pytest

from autodev.adapters.git import GitRepo
from autodev.config.models import DEFAULT_TEST_GLOBS, CommandsConfig, ProjectConfig
from autodev.gates.tdd import TddGuard, is_test_file
from tests.integration.conftest import git

# check.py exits non-zero unless the implementation returns 2.
CHECK = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
    "from impl import value\n"
    "sys.exit(0 if value() == 2 else 1)\n"
)
IMPL_BAD = "def value():\n    return 1\n"
IMPL_GOOD = "def value():\n    return 2\n"


def make_config(test: str | None) -> ProjectConfig:
    return ProjectConfig(commands=CommandsConfig(test=test))


@pytest.fixture
def project(tmp_path: Path) -> GitRepo:
    root = tmp_path / "proj"
    root.mkdir()
    repo = GitRepo(root)
    repo.init("main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    (root / "impl.py").write_text(IMPL_BAD)
    repo.commit_all("initial")
    return repo


@pytest.fixture
def guard(project: GitRepo) -> TddGuard:
    command = f"{shlex.quote(sys.executable)} -B tests/check.py"
    return TddGuard(make_config(command), project)


def commit(repo: GitRepo, files: dict[str, str], message: str = "c") -> str:
    for name, content in files.items():
        path = repo.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return repo.commit_all(message)


def test_is_test_file_matches_default_globs() -> None:
    for path in (
        "tests/x.py",
        "pkg/tests/x.py",
        "test_a.py",
        "src/test_a.py",
        "a_test.go",
        "a.spec.ts",
    ):
        assert is_test_file(path, DEFAULT_TEST_GLOBS), path
    for path in ("src/latest.py", "src/app.py"):
        assert not is_test_file(path, DEFAULT_TEST_GLOBS), path


def test_red_requires_test_command(project: GitRepo) -> None:
    sha = commit(project, {"tests/check.py": CHECK})
    guard = TddGuard(make_config(None), project)
    for result in (guard.check_red(sha), guard.check_green(sha)):
        assert not result.ok
        assert result.output == "TDD guard requires commands.test"
    assert guard.check_red(sha).name == "tdd-red"
    assert guard.check_green(sha).name == "tdd-green"


def test_red_passes_when_only_tests_changed_and_suite_fails(
    project: GitRepo, guard: TddGuard
) -> None:
    sha = commit(project, {"tests/check.py": CHECK})
    result = guard.check_red(sha)
    assert result.ok, result.output
    assert result.name == "tdd-red"


def test_red_fails_when_commit_touches_non_test_file(project: GitRepo, guard: TddGuard) -> None:
    sha = commit(project, {"tests/check.py": CHECK, "impl.py": IMPL_BAD + "# x\n"})
    result = guard.check_red(sha)
    assert not result.ok
    assert "impl.py" in result.output


def test_red_fails_when_suite_passes(project: GitRepo, guard: TddGuard) -> None:
    commit(project, {"impl.py": IMPL_GOOD})
    sha = commit(project, {"tests/check.py": CHECK})
    result = guard.check_red(sha)
    assert not result.ok
    assert "pass" in result.output.lower()


def test_red_fails_on_empty_commit(project: GitRepo, guard: TddGuard) -> None:
    git(project.path, "commit", "--allow-empty", "-m", "empty")
    result = guard.check_red(project.head())
    assert not result.ok
    assert "no files" in result.output.lower()


def test_green_passes_when_suite_passes_without_test_changes(
    project: GitRepo, guard: TddGuard
) -> None:
    commit(project, {"tests/check.py": CHECK})
    sha = commit(project, {"impl.py": IMPL_GOOD})
    result = guard.check_green(sha)
    assert result.ok, result.output
    assert result.name == "tdd-green"


def test_green_fails_when_commit_touches_test_file(project: GitRepo, guard: TddGuard) -> None:
    commit(project, {"tests/check.py": CHECK})
    sha = commit(project, {"impl.py": IMPL_GOOD, "tests/check.py": CHECK + "# edit\n"})
    result = guard.check_green(sha)
    assert not result.ok
    assert "tests/check.py" in result.output


def test_green_fails_when_suite_fails(project: GitRepo, guard: TddGuard) -> None:
    commit(project, {"tests/check.py": CHECK})
    sha = commit(project, {"impl.py": IMPL_BAD + "# still wrong\n"})
    result = guard.check_green(sha)
    assert not result.ok
