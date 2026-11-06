import json
import re
from pathlib import Path

from autodev.config import ConfigError, load_config
from autodev.pipeline.outputs import Commands

_KEYS = ("build", "test", "lint")
_BLOCK_START = re.compile(r"^commands:\s*(#.*)?$")
_DEFAULT_INDENT = "  "


def set_commands(project_root: Path, commands: Commands) -> None:
    """Rewrite build/test/lint in the `commands:` block of .agent/config.yaml, line by line."""
    path = project_root / ".agent" / "config.yaml"
    original = path.read_text()
    lines = original.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if _BLOCK_START.match(line))
    except StopIteration:
        lines += ["commands:"]
        start = len(lines) - 1
    for key in _KEYS:
        value = getattr(commands, key)
        lines = _set_key(lines, start, key, "null" if value is None else _quote(value))
    path.write_text("\n".join(lines) + "\n")
    try:
        load_config(project_root)
    except ConfigError:
        path.write_text(original)
        raise


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _block_end(lines: list[str], start: int) -> int:
    """Index just after the last non-blank line that belongs to the block."""
    end = start + 1
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if not line[0].isspace():
            break
        end = i + 1
    return end


def _set_key(lines: list[str], start: int, key: str, value: str) -> list[str]:
    end = _block_end(lines, start)
    pattern = re.compile(rf"^(\s+{key}:\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^#]*?)(\s*)(#.*)?$")
    for i in range(start + 1, end):
        if match := pattern.match(lines[i]):
            comment = match.group(4) or ""
            gap = match.group(3) if comment else ""
            lines[i] = f"{match.group(1)}{value}{gap}{comment}"
            return lines
    indent = next(
        (m.group(1) for line in lines[start + 1 : end] if (m := re.match(r"^(\s+)\S", line))),
        _DEFAULT_INDENT,
    )
    lines.insert(end, f"{indent}{key}: {value}")
    return lines
