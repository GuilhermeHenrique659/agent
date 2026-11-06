from pathlib import Path

from autodev.pipeline.adr import adr_dir, has_decisions, write_adr
from autodev.pipeline.outputs import AdrDraft

TEMPLATE = "# ADR 0000: Title\n"


def make_root(tmp_path: Path) -> Path:
    (tmp_path / ".agent" / "architecture" / "adr").mkdir(parents=True)
    (adr_dir(tmp_path) / "0000-template.md").write_text(TEMPLATE)
    return tmp_path


def draft(title: str = "Use PostgreSQL") -> AdrDraft:
    return AdrDraft(title=title, context="Need a DB", decision="Postgres", consequences="Ops cost")


def test_has_decisions_ignores_template(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    assert not has_decisions(root)
    (adr_dir(root) / "0001-x.md").write_text("x")
    assert has_decisions(root)


def test_has_decisions_false_without_adr_dir(tmp_path: Path) -> None:
    assert not has_decisions(tmp_path)


def test_write_adr_numbers_and_slugs(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    first = write_adr(root, draft("Escolha: Armazenamento & Cache -- Não!"))
    assert first == adr_dir(root) / "0001-escolha-armazenamento-cache-nao.md"
    second = write_adr(root, draft("Use PostgreSQL"))
    assert second.name == "0002-use-postgresql.md"
    long = write_adr(root, draft("a" * 80))
    assert long.name == f"0003-{'a' * 50}.md"


def test_write_adr_content_follows_template_sections(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    path = write_adr(root, draft(), "Proposed")
    assert path.read_text() == (
        "# ADR 0001: Use PostgreSQL\n\n## Status\n\nProposed\n\n## Context\n\nNeed a DB\n\n"
        "## Decision\n\nPostgres\n\n## Consequences\n\nOps cost\n"
    )
