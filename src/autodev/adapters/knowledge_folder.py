from collections.abc import Sequence
from pathlib import Path

from autodev.config.models import ProjectConfig

_DIRS = (".agent/knowledge", ".agent/standards", ".agent/architecture/adr")
_FILES = (
    ".agent/domain/glossary.md",
    ".agent/domain/events.md",
    "CLAUDE.md",
    "AGENTS.md",
)
_RULES_DIR = ".claude/rules"
_DOC_SUFFIXES = (".md", ".txt", ".rst", ".adoc")


class KnowledgeFolder:
    def __init__(self, project_root: Path, extra_paths: Sequence[str] = ()) -> None:
        self._root = project_root
        self._extra = tuple(extra_paths)

    def paths(self) -> list[Path]:
        found = [
            p
            for d in _DIRS
            for p in (self._root / d).rglob("*")
            if p.is_file() and p.name != ".gitkeep"
        ]
        found += [self._root / f for f in _FILES if (self._root / f).is_file()]
        found += (self._root / _RULES_DIR).rglob("*.md")
        for extra in self._extra:
            found += self._extra_files(self._root / extra)
        return sorted({p.relative_to(self._root) for p in found if p.is_file()})

    def _extra_files(self, target: Path) -> list[Path]:
        if target.is_file():
            return [target]
        if not target.is_dir():
            return []
        return [
            p
            for p in target.rglob("*")
            if p.suffix.lower() in _DOC_SUFFIXES
            and not any(part.startswith(".") for part in p.relative_to(target).parts)
        ]


def make_context(config: ProjectConfig, path: Path) -> KnowledgeFolder:
    """Context source for a project: the fixed knowledge files plus `context.paths`."""
    return KnowledgeFolder(path, config.context.paths)
