import re
from collections.abc import Collection, Sequence
from pathlib import Path

from autodev.gates.runner import GateReport
from autodev.gates.tdd import is_test_file

# `path:line:col` suffixes are cut naturally: ":" is not part of a token.
_TOKEN = re.compile(r"[A-Za-z0-9_./\\-]+")


def gate_failure_files(
    report: GateReport, root: Path, globs: Sequence[str], tracked: Collection[str]
) -> tuple[list[str], list[str]]:
    """Tracked repo files named in the output of the failing gates: (test files, other files).

    Only tracked files count: ignored or untracked paths (`.venv/`, caches) mentioned in
    warnings say nothing about which code the failure involves.
    """
    found: set[str] = set()
    base = root.resolve()
    for result in report.failures:
        for token in _TOKEN.findall(result.output):
            candidate = (base / token).resolve()
            if not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(base).as_posix()
            except ValueError:
                continue
            if relative in tracked:
                found.add(relative)
    tests = sorted(f for f in found if is_test_file(f, globs))
    others = sorted(f for f in found if not is_test_file(f, globs))
    return tests, others
