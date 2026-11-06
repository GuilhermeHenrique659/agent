from pathlib import Path

from autodev.pipeline.domain_docs import append_event_storming, merge_glossary
from autodev.pipeline.outputs import EventStorming, GlossaryEntry

HEADER = "| Term | Definition | Notes |\n|------|------------|-------|\n"


def test_merge_glossary_appends_new_terms(tmp_path: Path) -> None:
    path = tmp_path / "glossary.md"
    path.write_text(f"# Glossary\n\n{HEADER}| Order | A purchase | |\n")
    merge_glossary(path, [GlossaryEntry(term="Cart", definition="Items", notes="temp")])
    lines = path.read_text().splitlines()
    assert lines[-2:] == ["| Order | A purchase | |", "| Cart | Items | temp |"]


def test_merge_glossary_updates_existing_term_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "glossary.md"
    path.write_text(f"{HEADER}| Order | old | n |\n| Cart | keep | |\n")
    merge_glossary(path, [GlossaryEntry(term="order", definition="new", notes="")])
    lines = path.read_text().splitlines()
    assert lines[2:] == ["| Order | new |  |", "| Cart | keep | |"]


def test_merge_glossary_creates_file_with_header(tmp_path: Path) -> None:
    path = tmp_path / "domain" / "glossary.md"
    merge_glossary(path, [GlossaryEntry(term="Cart", definition="Items")])
    lines = path.read_text().splitlines()
    assert lines[0] == "| Term | Definition | Notes |"
    assert lines[-1] == "| Cart | Items |  |"


def test_merge_glossary_keeps_other_text_and_escapes_pipes(tmp_path: Path) -> None:
    path = tmp_path / "glossary.md"
    path.write_text(f"# Glossary\n\nintro\n\n{HEADER}| A | b | |\n\nfooter text\n")
    merge_glossary(path, [GlossaryEntry(term="X|Y", definition="a|b", notes="c")])
    text = path.read_text()
    assert text.startswith("# Glossary\n\nintro\n\n")
    assert "| X\\|Y | a\\|b | c |" in text
    assert text.index("| A | b | |") < text.index("| X\\|Y")
    assert text.rstrip().endswith("footer text") or "footer text" in text
    # file without the header gets the table appended
    other = tmp_path / "other.md"
    other.write_text("# Notes\n")
    merge_glossary(other, [GlossaryEntry(term="T", definition="D")])
    assert other.read_text().startswith("# Notes\n")
    assert "| Term | Definition | Notes |" in other.read_text()
    assert "| T | D |  |" in other.read_text()


def test_append_event_storming_writes_section_omitting_empty_lists(tmp_path: Path) -> None:
    path = tmp_path / "domain" / "events.md"
    es = EventStorming(domain_events=["OrderPlaced"], actors=["Customer", "Admin"])
    append_event_storming(path, "S-0001", "Checkout", es)
    text = path.read_text()
    assert "## S-0001: Checkout" in text
    assert "### Domain events\n- OrderPlaced" in text
    assert "### Actors\n- Customer\n- Admin" in text
    assert "### Commands" not in text
    assert "### Read models" not in text
    append_event_storming(path, "S-0002", "Other", EventStorming(commands=["Pay"]))
    assert path.read_text().startswith(text.rstrip("\n"))
    assert "## S-0002: Other" in path.read_text()
