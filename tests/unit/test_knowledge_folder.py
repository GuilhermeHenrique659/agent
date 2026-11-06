from pathlib import Path

from autodev.adapters.knowledge_folder import KnowledgeFolder


def test_lists_existing_context_files_sorted_and_skips_gitkeep(tmp_path: Path) -> None:
    agent = tmp_path / ".agent"
    for rel in (
        "knowledge/b.md",
        "knowledge/a/deep.md",
        "knowledge/.gitkeep",
        "standards/s.md",
        "architecture/adr/0001.md",
        "domain/glossary.md",
        "backlog/S-0001.md",
    ):
        path = agent / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    assert KnowledgeFolder(tmp_path).paths() == [
        Path(".agent/architecture/adr/0001.md"),
        Path(".agent/domain/glossary.md"),
        Path(".agent/knowledge/a/deep.md"),
        Path(".agent/knowledge/b.md"),
        Path(".agent/standards/s.md"),
    ]


def test_missing_folders_yield_empty_list(tmp_path: Path) -> None:
    assert KnowledgeFolder(tmp_path).paths() == []
