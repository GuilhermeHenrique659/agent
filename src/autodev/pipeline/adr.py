import re
import unicodedata
from pathlib import Path
from typing import Literal

from autodev.pipeline.outputs import AdrDraft

_TEMPLATE = "0000-template.md"
_SLUG_CHARS = 50
_NUMBER_RE = re.compile(r"^(\d{4})-")


def adr_dir(project_root: Path) -> Path:
    return project_root / ".agent" / "architecture" / "adr"


def has_decisions(project_root: Path) -> bool:
    directory = adr_dir(project_root)
    return directory.is_dir() and any(p.name != _TEMPLATE for p in directory.glob("*.md"))


def write_adr(
    project_root: Path, draft: AdrDraft, status: Literal["Proposed", "Accepted"] = "Accepted"
) -> Path:
    directory = adr_dir(project_root)
    directory.mkdir(parents=True, exist_ok=True)
    numbers = [int(m.group(1)) for p in directory.glob("*.md") if (m := _NUMBER_RE.match(p.name))]
    number = f"{max(numbers, default=0) + 1:04d}"
    path = directory / f"{number}-{_slug(draft.title)}.md"
    path.write_text(
        f"# ADR {number}: {draft.title}\n\n## Status\n\n{status}\n\n"
        f"## Context\n\n{draft.context.strip()}\n\n## Decision\n\n{draft.decision.strip()}\n\n"
        f"## Consequences\n\n{draft.consequences.strip()}\n"
    )
    return path


def _slug(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:_SLUG_CHARS].strip("-") or "decision"
