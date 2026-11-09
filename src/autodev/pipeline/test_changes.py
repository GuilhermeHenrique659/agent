from collections.abc import Sequence

from autodev.gates.tdd import is_test_file

DEFAULT_LIMIT = 12000
_FILE_START = "diff --git "
_TRUNCATED = "... (truncated)"


def _sections(diff_text: str) -> list[list[str]]:
    sections: list[list[str]] = []
    for line in diff_text.splitlines():
        if line.startswith(_FILE_START):
            sections.append([])
        if sections:
            sections[-1].append(line)
    return sections


def _path(section: list[str]) -> str:
    return section[0].rsplit(" b/", 1)[-1]


def _split_header(section: list[str]) -> tuple[list[str], list[str]]:
    """File header (up to the `+++` line) and the hunks."""
    for i, line in enumerate(section):
        if line.startswith("+++ "):
            return section[: i + 1], section[i + 1 :]
    return section, []


def diff_of_test_files(diff_text: str, globs: Sequence[str]) -> str:
    """The parts of a unified diff that belong to test files."""
    kept = [s for s in _sections(diff_text) if is_test_file(_path(s), globs)]
    return "\n".join("\n".join(s) for s in kept)


def truncate_diff(diff_text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Cut the diff to about `limit` characters, keeping every file header."""
    if len(diff_text) <= limit:
        return diff_text
    parts = [_split_header(s) for s in _sections(diff_text)]
    budget = max(limit - sum(len("\n".join(h)) + 1 for h, _ in parts), 0)
    out: list[str] = []
    for header, body in parts:
        kept: list[str] = []
        for line in body:
            if len(line) + 1 > budget:
                kept.append(_TRUNCATED)
                budget = 0
                break
            kept.append(line)
            budget -= len(line) + 1
        out.append("\n".join(header + kept))
    return "\n".join(out)
