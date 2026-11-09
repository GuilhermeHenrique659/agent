import xml.etree.ElementTree as ET
from enum import StrEnum
from pathlib import Path

from autodev.config.models import ProjectConfig
from autodev.gates.runner import GateResult, run_command


class JunitReportError(Exception):
    pass


class TestOutcome(StrEnum):
    __test__ = False  # not a pytest test class

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


def _outcome(case: ET.Element) -> TestOutcome:
    if case.find("failure") is not None:
        return TestOutcome.FAILED
    if case.find("error") is not None:
        return TestOutcome.ERROR
    if case.find("skipped") is not None:
        return TestOutcome.SKIPPED
    return TestOutcome.PASSED


def parse_junit(path: Path) -> dict[str, TestOutcome]:
    """Outcome per test id (`classname::name`, or `name` without a classname)."""
    if not path.is_file():
        raise JunitReportError(f"JUnit report missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise JunitReportError(f"JUnit report invalid: {path}: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise JunitReportError(f"JUnit report invalid: {path}: unexpected root <{root.tag}>")
    outcomes: dict[str, TestOutcome] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        outcomes[f"{classname}::{name}" if classname else name] = _outcome(case)
    return outcomes


def run_tests_with_report(
    config: ProjectConfig, root: Path
) -> tuple[GateResult, dict[str, TestOutcome] | None]:
    """Run `commands.test`; when `tdd.junit_report` is set, also return the per-test outcomes.

    A configured report that the run did not write (or wrote invalid) turns the result into a
    failure with a clear message.
    """
    command = config.commands.test
    if command is None:
        return GateResult("test", True, "No command configured", skipped=True), None
    report = config.tdd.junit_report
    if report is None:
        code, output = run_command(command, root, config.limits.command_timeout_s)
        return GateResult("test", code == 0, output), None
    path = root / report
    path.unlink(missing_ok=True)
    code, output = run_command(command, root, config.limits.command_timeout_s)
    result = GateResult("test", code == 0, output)
    if code == -1:  # timeout: the report, if any, is incomplete
        return result, None
    try:
        return result, parse_junit(path)
    except JunitReportError as exc:
        message = (
            f"test command did not write the JUnit report at {report}"
            if not path.is_file()
            else str(exc)
        )
        return GateResult("test", False, f"{message}\n{output}"), None
