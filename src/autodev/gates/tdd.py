from collections.abc import Sequence
from fnmatch import fnmatchcase

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.gates.runner import GateResult, run_command

_NO_TEST_COMMAND = "TDD guard requires commands.test"


def is_test_file(path: str, globs: Sequence[str]) -> bool:
    return any(fnmatchcase(path, g) for g in globs)


class TddGuard:
    """Verifies red/green commits.

    The test command runs against the current working tree of `repo`, so callers
    must check out the commit before calling check_red / check_green.
    """

    def __init__(self, config: ProjectConfig, repo: GitRepo) -> None:
        self._config = config
        self._repo = repo

    def check_red(self, commit: str) -> GateResult:
        name = "tdd-red"
        command = self._config.commands.test
        if command is None:
            return GateResult(name, False, _NO_TEST_COMMAND)
        files = self._repo.changed_files(commit)
        if not files:
            return GateResult(name, False, "Red commit changes no files; it must add tests")
        globs = self._config.tdd.test_globs
        others = [f for f in files if not is_test_file(f, globs)]
        if others:
            listed = "\n".join(others)
            return GateResult(
                name, False, f"Red commit may only touch test files; non-test files:\n{listed}"
            )
        code, output = run_command(command, self._repo.path, self._config.limits.command_timeout_s)
        if code == 0:
            return GateResult(name, False, f"Tests passed but must fail in the red phase\n{output}")
        if code == -1:
            return GateResult(name, False, output)
        return GateResult(name, True, output)

    def check_green(self, commit: str) -> GateResult:
        name = "tdd-green"
        command = self._config.commands.test
        if command is None:
            return GateResult(name, False, _NO_TEST_COMMAND)
        globs = self._config.tdd.test_globs
        tests = [f for f in self._repo.changed_files(commit) if is_test_file(f, globs)]
        if tests:
            listed = "\n".join(tests)
            return GateResult(
                name, False, f"Green commit may not touch test files; test files changed:\n{listed}"
            )
        code, output = run_command(command, self._repo.path, self._config.limits.command_timeout_s)
        return GateResult(name, code == 0, output)
