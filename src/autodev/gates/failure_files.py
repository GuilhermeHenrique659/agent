import re
from collections.abc import Sequence
from pathlib import Path

from autodev.gates.runner import GateReport
from autodev.gates.tdd import is_test_file

# `path:line:col` suffixes are cut naturally: ":" is not part of a token.
_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+")


def gate_failure_files(
    report: GateReport, root: Path, globs: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Existing repo files named in the output of the failing gates: (test files, other files)."""
    found: set[str] = set()
    base = root.resolve()
    for result in report.failures:
        for token in _TOKEN.findall(result.output):
            candidate = (base / token).resolve()
            if not candidate.is_file():
                continue
            try:
                found.add(candidate.relative_to(base).as_posix())
            except ValueError:
                continue
    tests = sorted(f for f in found if is_test_file(f, globs))
    others = sorted(f for f in found if not is_test_file(f, globs))
    return tests, others
