from pathlib import Path

from autodev.config.models import CommandsConfig, LimitsConfig, ProjectConfig
from autodev.gates.runner import CommandGate, GateReport, GateResult, run_gates


def make_config(
    build: str | None = None,
    test: str | None = None,
    lint: str | None = None,
    timeout: int = 30,
) -> ProjectConfig:
    return ProjectConfig(
        commands=CommandsConfig(build=build, test=test, lint=lint),
        limits=LimitsConfig(command_timeout_s=timeout),
    )


def test_gate_without_command_is_skipped_and_ok(tmp_path: Path) -> None:
    result = CommandGate("build", None, 5).check(tmp_path)
    assert result == GateResult("build", True, "No command configured", skipped=True)


def test_gate_passes_on_exit_zero(tmp_path: Path) -> None:
    result = CommandGate("test", "exit 0", 5).check(tmp_path)
    assert result.ok
    assert not result.skipped


def test_gate_fails_on_nonzero_exit_and_captures_output(tmp_path: Path) -> None:
    result = CommandGate("lint", "echo out; echo err >&2; exit 3", 5).check(tmp_path)
    assert not result.ok
    assert "out" in result.output
    assert "err" in result.output


def test_gate_times_out_as_failure(tmp_path: Path) -> None:
    result = CommandGate("test", "sleep 5", 1).check(tmp_path)
    assert not result.ok
    assert "timed out after 1s" in result.output


def test_run_gates_runs_build_test_lint_in_order(tmp_path: Path) -> None:
    config = make_config("echo b >> log", "echo t >> log", "echo l >> log")
    report = run_gates(config, tmp_path)
    assert report.ok
    assert [r.name for r in report.results] == ["build", "test", "lint"]
    assert (tmp_path / "log").read_text().split() == ["b", "t", "l"]


def test_run_gates_stops_at_first_failure(tmp_path: Path) -> None:
    config = make_config("exit 0", "exit 1", "touch lint-ran")
    report = run_gates(config, tmp_path)
    assert not report.ok
    assert [r.name for r in report.results] == ["build", "test"]
    assert [r.name for r in report.failures] == ["test"]
    assert not (tmp_path / "lint-ran").exists()


def test_report_ok_and_failures() -> None:
    good = GateResult("build", True, "")
    bad = GateResult("test", False, "x")
    assert GateReport([good]).ok
    assert GateReport([]).ok
    report = GateReport([good, bad])
    assert not report.ok
    assert report.failures == [bad]
