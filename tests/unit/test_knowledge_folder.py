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


def test_extra_paths_files_and_directories_filtered_by_extension_skipping_hidden(
    tmp_path: Path,
) -> None:
    for rel in (
        "README.md",
        "docs/a.md",
        "docs/sub/b.rst",
        "docs/sub/c.txt",
        "docs/d.adoc",
        "docs/img.png",
        "docs/code.py",
        "docs/.hidden/e.md",
        "doc/f.md",
        "NOTES",
        ".agent/standards/s.md",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    folder = KnowledgeFolder(tmp_path, ["README.md", "docs", "docs/", "missing", "NOTES"])
    assert folder.paths() == [
        Path(".agent/standards/s.md"),
        Path("NOTES"),
        Path("README.md"),
        Path("docs/a.md"),
        Path("docs/d.adoc"),
        Path("docs/sub/b.rst"),
        Path("docs/sub/c.txt"),
    ]
