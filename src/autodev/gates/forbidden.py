import re
from collections.abc import Sequence

from autodev.gates.tdd import is_test_file

_HUNK = re.compile(r"^@@ -\S+ \+(\d+)")


def forbidden_additions(diff_text: str, patterns: Sequence[str], globs: Sequence[str]) -> list[str]:
    """Lines added to non-test files (from a `git diff -U0`) that match a forbidden pattern.

    Each offender is reported as `path:line: text`.
    """
    regexes = [re.compile(p) for p in patterns]
    found: list[str] = []
    path: str | None = None
    line_no = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else None
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if match := _HUNK.match(raw):
            line_no = int(match.group(1))
            continue
        if raw.startswith("+") and path is not None:
            text = raw[1:]
            if not is_test_file(path, globs) and any(r.search(text) for r in regexes):
                found.append(f"{path}:{line_no}: {text}")
            line_no += 1
    return found
