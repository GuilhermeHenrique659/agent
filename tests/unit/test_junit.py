import shlex
import sys
from pathlib import Path

import pytest

from autodev.config.models import CommandsConfig, ProjectConfig, TddConfig
from autodev.gates.junit import JunitReportError, TestOutcome, parse_junit, run_tests_with_report

CASES = """
<testcase classname="tests.test_a" name="test_ok"/>
<testcase classname="tests.test_a" name="test_bad"><failure message="x">trace</failure></testcase>
<testcase classname="tests.test_a" name="test_err"><error message="x">trace</error></testcase>
<testcase classname="tests.test_a" name="test_skip"><skipped message="x"/></testcase>
<testcase name="bare"/>
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(text)
    return path


def test_parses_passed_failed_error_skipped(tmp_path: Path) -> None:
    path = write(tmp_path, f'<testsuites><testsuite name="s">{CASES}</testsuite></testsuites>')
    assert parse_junit(path) == {
        "tests.test_a::test_ok": TestOutcome.PASSED,
        "tests.test_a::test_bad": TestOutcome.FAILED,
        "tests.test_a::test_err": TestOutcome.ERROR,
        "tests.test_a::test_skip": TestOutcome.SKIPPED,
        "bare": TestOutcome.PASSED,
    }


def test_handles_testsuites_root_and_single_testsuite(tmp_path: Path) -> None:
    single = write(tmp_path, '<testsuite><testcase classname="c" name="t"/></testsuite>')
    assert parse_junit(single) == {"c::t": TestOutcome.PASSED}
    nested = write(
        tmp_path,
        '<testsuites><testsuite><testcase classname="c" name="a"/></testsuite>'
        '<testsuite><testcase classname="c" name="b"><failure/></testcase></testsuite>'
        "</testsuites>",
    )
    assert parse_junit(nested) == {"c::a": TestOutcome.PASSED, "c::b": TestOutcome.FAILED}


def test_collection_error_maps_to_error(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '<testsuites><testsuite><testcase classname="" name="tests.test_new">'
        '<error message="collection failure">ImportError</error></testcase>'
        "</testsuite></testsuites>",
    )
    assert parse_junit(path) == {"tests.test_new": TestOutcome.ERROR}


def test_missing_or_invalid_file_raises(tmp_path: Path) -> None:
    with pytest.raises(JunitReportError, match="missing"):
        parse_junit(tmp_path / "nope.xml")
    with pytest.raises(JunitReportError, match="invalid"):
        parse_junit(write(tmp_path, "<testsuite><oops"))
    with pytest.raises(JunitReportError, match="invalid"):
        parse_junit(write(tmp_path, "<html/>"))


def project_config(command: str, report: str | None) -> ProjectConfig:
    return ProjectConfig(commands=CommandsConfig(test=command), tdd=TddConfig(junit_report=report))


def python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_run_with_report_parses_report_even_when_tests_fail(tmp_path: Path) -> None:
    code = (
        "import sys;open('r.xml','w').write("
        '\'<testsuite><testcase classname="c" name="t"><failure/></testcase></testsuite>\');'
        "sys.exit(1)"
    )
    result, outcomes = run_tests_with_report(
        project_config(python_command(code), "r.xml"), tmp_path
    )
    assert not result.ok
    assert outcomes == {"c::t": TestOutcome.FAILED}


def test_run_with_report_deletes_stale_report_and_flags_missing_one(tmp_path: Path) -> None:
    (tmp_path / "r.xml").write_text("<testsuite/>")
    result, outcomes = run_tests_with_report(
        project_config(python_command("pass"), "r.xml"), tmp_path
    )
    assert outcomes is None
    assert not result.ok
    assert "did not write the JUnit report at r.xml" in result.output


def test_run_with_report_without_configured_report_returns_no_outcomes(tmp_path: Path) -> None:
    result, outcomes = run_tests_with_report(project_config(python_command("pass"), None), tmp_path)
    assert result.ok and outcomes is None
