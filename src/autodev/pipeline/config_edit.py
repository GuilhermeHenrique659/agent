import json
import re
from pathlib import Path

from autodev.config import ConfigError, load_config
from autodev.pipeline.outputs import Commands

type Value = str | int | float | list[str] | None

_KEYS = ("build", "test", "lint")
_STEP = "  "


def set_commands(project_root: Path, commands: Commands) -> None:
    """Rewrite build/test/lint in the `commands:` block of .agent/config.yaml, line by line."""
    _edit(project_root, [(f"commands.{key}", getattr(commands, key)) for key in _KEYS])


def set_value(project_root: Path, key: str, value: Value) -> None:
    """Set a dotted key (1-3 levels) in .agent/config.yaml, preserving comments and layout."""
    _edit(project_root, [(key, value)])


def _edit(project_root: Path, edits: list[tuple[str, Value]]) -> None:
    path = project_root / ".agent" / "config.yaml"
    original = path.read_text()
    lines = original.splitlines()
    for key, value in edits:
        lines = _set_key(lines, key.split("."), _render(value))
    path.write_text("\n".join(lines) + "\n")
    try:
        load_config(project_root)
    except ConfigError:
        path.write_text(original)
        raise


def _render(value: Value) -> str:
    if value is None:
        return "null"
    if isinstance(value, list):
        return "[" + ", ".join(_quote(v) for v in value) + "]"
    if isinstance(value, str):
        return _quote(value)
    return repr(value)


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: list[str], start: int) -> int:
    """Index just after the last non-blank line nested under the line at `start`."""
    parent = _indent_of(lines[start])
    end = start + 1
    for i in range(start + 1, len(lines)):
        if not lines[i].strip():
            continue
        if _indent_of(lines[i]) <= parent:
            break
        end = i + 1
    return end


def _find(
    lines: list[str], lo: int, hi: int, indent: int, name: str
) -> tuple[int, re.Match[str]] | None:
    pattern = re.compile(
        rf"^( {{{indent}}}{re.escape(name)}:\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^#]*?)(\s*)(#.*)?$"
    )
    for i in range(lo, hi):
        if match := pattern.match(lines[i]):
            return i, match
    return None


def _set_key(lines: list[str], parts: list[str], value: str) -> list[str]:
    lo, hi, indent = 0, len(lines), 0
    for depth, part in enumerate(parts):
        last = depth == len(parts) - 1
        hit = _find(lines, lo, hi, indent, part)
        if hit is None:
            return _insert(lines, parts[depth:], value, hi, indent)
        index, match = hit
        if last:
            comment = match.group(4) or ""
            gap = match.group(3) if comment else ""
            lines[index] = f"{match.group(1)}{value}{gap}{comment}"
            return lines
        hi = _block_end(lines, index)
        lo = index + 1
        indent = next(
            (_indent_of(line) for line in lines[lo:hi] if line.strip()), indent + len(_STEP)
        )
    return lines


def _insert(lines: list[str], parts: list[str], value: str, at: int, indent: int) -> list[str]:
    new = [f"{' ' * (indent + len(_STEP) * level)}{part}:" for level, part in enumerate(parts[:-1])]
    new.append(f"{' ' * (indent + len(_STEP) * (len(parts) - 1))}{parts[-1]}: {value}")
    return lines[:at] + new + lines[at:]
