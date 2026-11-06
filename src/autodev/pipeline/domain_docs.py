from pathlib import Path

from autodev.pipeline.outputs import EventStorming, GlossaryEntry

_HEADER = "| Term | Definition | Notes |"
_SEPARATOR = "|------|------------|-------|"
_TABLE_HEAD = f"{_HEADER}\n{_SEPARATOR}\n"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _row(term: str, entry: GlossaryEntry) -> str:
    return f"| {term} | {_cell(entry.definition)} | {_cell(entry.notes)} |"


def _split_cells(line: str) -> list[str]:
    """Split a table row on unescaped pipes."""
    cells: list[str] = []
    current = ""
    chars = iter(line.strip())
    for ch in chars:
        if ch == "\\":
            current += ch + next(chars, "")
        elif ch == "|":
            cells.append(current.strip())
            current = ""
        else:
            current += ch
    cells.append(current.strip())
    return cells[1:] if cells and cells[0] == "" else cells


def merge_glossary(path: Path, entries: list[GlossaryEntry]) -> None:
    text = path.read_text() if path.is_file() else ""
    lines = text.splitlines()
    header_at = next((i for i, ln in enumerate(lines) if ln.strip() == _HEADER), None)
    if header_at is None:
        if lines:
            lines.append("")
        lines += _TABLE_HEAD.splitlines()
        header_at = len(lines) - 2
    # the table body: consecutive rows after header and separator
    start = header_at + 2
    end = start
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    rows = lines[start:end]
    for entry in entries:
        key = _cell(entry.term).casefold()
        for i, row in enumerate(rows):
            cells = _split_cells(row)
            if cells and cells[0].casefold() == key:
                rows[i] = _row(cells[0], entry)
                break
        else:
            rows.append(_row(_cell(entry.term), entry))
    lines[start:end] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def append_event_storming(path: Path, story_id: str, title: str, es: EventStorming) -> None:
    sections = [
        ("Domain events", es.domain_events),
        ("Commands", es.commands),
        ("Actors", es.actors),
        ("Aggregates", es.aggregates),
        ("Policies", es.policies),
        ("Read models", es.read_models),
    ]
    parts = [f"## {story_id}: {title}"]
    for name, items in sections:
        if items:
            bullets = "\n".join(f"- {item}" for item in items)
            parts.append(f"### {name}\n{bullets}")
    existing = path.read_text().rstrip("\n") if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{existing}\n\n" if existing else ""
    path.write_text(prefix + "\n\n".join(parts) + "\n")
