from pathlib import Path

from autodev.pipeline.merge import files_with_conflict_markers


def test_files_with_conflict_markers(tmp_path: Path) -> None:
    (tmp_path / "start.txt").write_text("a\n<<<<<<< HEAD\nb\n")
    (tmp_path / "end.txt").write_text("a\n>>>>>>> story/S-0001\n")
    (tmp_path / "bare.txt").write_text("<<<<<<<\nx\n")
    (tmp_path / "bare_end.txt").write_text("x\n>>>>>>>\n")
    (tmp_path / "plain.txt").write_text("title\n=======\nmore\n")
    (tmp_path / "clean.txt").write_text("nothing here\n")
    (tmp_path / "inline.txt").write_text("x <<<<<<< not at line start\n")
    files = ["start.txt", "end.txt", "bare.txt", "bare_end.txt", "plain.txt", "clean.txt"]
    assert files_with_conflict_markers(tmp_path, [*files, "inline.txt", "gone.txt"]) == [
        "start.txt",
        "end.txt",
        "bare.txt",
        "bare_end.txt",
    ]
