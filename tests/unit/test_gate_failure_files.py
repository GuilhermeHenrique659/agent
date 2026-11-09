from pathlib import Path

from autodev.config.models import DEFAULT_TEST_GLOBS
from autodev.gates.failure_files import gate_failure_files
from autodev.gates.runner import GateReport, GateResult


def make_root(tmp_path: Path) -> Path:
    for name in ("tests/test_a.py", "tests/test_b.py", "src/app.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    return tmp_path


def tracked_in(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_extracts_existing_paths_with_line_numbers_and_classifies(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    output = (
        "tests/test_a.py:12:5: E501 line too long\n"
        "Would reformat: ./tests/test_b.py\n"
        "  --> src/app.py:3\n"
    )
    report = GateReport(
        [GateResult("build", True, "src/app.py"), GateResult("lint", False, output)]
    )
    tests, others = gate_failure_files(report, root, DEFAULT_TEST_GLOBS, tracked_in(root))
    assert tests == ["tests/test_a.py", "tests/test_b.py"]
    assert others == ["src/app.py"]


def test_ignores_tokens_that_are_not_existing_files(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    output = "ruff check . failed: tests/missing.py:1 E501 1.5 ../outside.py tests/ src\n"
    report = GateReport([GateResult("lint", False, output)])
    assert gate_failure_files(report, root, DEFAULT_TEST_GLOBS, tracked_in(root)) == ([], [])


def test_untracked_or_ignored_paths_are_not_counted(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    ignored = root / ".venv/lib/site-packages/fastapi/testclient.py"
    ignored.parent.mkdir(parents=True)
    ignored.write_text("x\n")
    output = (
        "tests/test_a.py:12:5: E501 line too long\n"
        ".venv/lib/site-packages/fastapi/testclient.py:3: DeprecationWarning\n"
    )
    report = GateReport([GateResult("test", False, output)])
    tracked = {"tests/test_a.py", "tests/test_b.py", "src/app.py"}
    tests, others = gate_failure_files(report, root, DEFAULT_TEST_GLOBS, tracked)
    assert tests == ["tests/test_a.py"]
    assert others == []
