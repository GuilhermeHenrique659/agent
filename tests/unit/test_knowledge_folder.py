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


def test_includes_claude_md_agents_md_and_rules(tmp_path: Path) -> None:
    for rel in (
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/rules/a.md",
        ".claude/rules/sub/b.md",
        ".claude/rules/notes.txt",
        ".agent/standards/s.md",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    assert KnowledgeFolder(tmp_path).paths() == [
        Path(".agent/standards/s.md"),
        Path(".claude/rules/a.md"),
        Path(".claude/rules/sub/b.md"),
        Path("AGENTS.md"),
        Path("CLAUDE.md"),
    ]
