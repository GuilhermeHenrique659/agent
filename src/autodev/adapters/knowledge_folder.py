from pathlib import Path

_DIRS = (".agent/knowledge", ".agent/standards", ".agent/architecture/adr")
_FILES = (
    ".agent/domain/glossary.md",
    ".agent/domain/events.md",
    "CLAUDE.md",
    "AGENTS.md",
)
_RULES_DIR = ".claude/rules"


class KnowledgeFolder:
    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def paths(self) -> list[Path]:
        found = [
            p
            for d in _DIRS
            for p in (self._root / d).rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        ]
        found += [self._root / f for f in _FILES if (self._root / f).is_file()]
        found += (self._root / _RULES_DIR).rglob("*.md")
        return sorted({p.relative_to(self._root) for p in found if p.is_file()})
