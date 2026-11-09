from collections.abc import Sequence
from fnmatch import fnmatchcase

from autodev.adapters.git import GitRepo
from autodev.config.models import ProjectConfig
from autodev.gates.junit import TestOutcome, run_tests_with_report
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

    def check_red_files(self, commit: str) -> GateResult:
        """The file rules of a red commit: at least one file, only test files."""
        name = "tdd-red"
        if self._config.commands.test is None:
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
        return GateResult(name, True, "")

    def check_red(self, commit: str) -> GateResult:
        name = "tdd-red"
        command = self._config.commands.test
        if command is None:
            return GateResult(name, False, _NO_TEST_COMMAND)
        files_ok = self.check_red_files(commit)
        if not files_ok.ok:
            return files_ok
        code, output = run_command(command, self._repo.path, self._config.limits.command_timeout_s)
        if code == 0:
            return GateResult(name, False, f"Tests passed but must fail in the red phase\n{output}")
        if code == -1:
            return GateResult(name, False, output)
        return GateResult(name, True, output)

    def check_green(
        self, commit: str, required_passing: frozenset[str] = frozenset()
    ) -> GateResult:
        """The suite passes and no test file changed; with a JUnit report, every id in
        `required_passing` must also be reported as passed."""
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
        result, outcomes = run_tests_with_report(self._config, self._repo.path)
        if not result.ok or outcomes is None:
            return GateResult(name, result.ok, result.output)
        not_passed = sorted(
            f"{test} ({outcomes[test].value if test in outcomes else 'missing from the report'})"
            for test in required_passing
            if outcomes.get(test) != TestOutcome.PASSED
        )
        if not_passed:
            listed = "\n".join(not_passed)
            return GateResult(
                name, False, f"These new tests of the task did not pass:\n{listed}\n{result.output}"
            )
        return GateResult(name, True, result.output)
