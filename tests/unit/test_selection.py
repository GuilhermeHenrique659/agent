from autodev.improve.selection import ask_selection, parse_selection
from tests.fakes import FakeIO


def test_parse_numbers_all_none_and_invalid() -> None:
    assert parse_selection("1, ,3", 3) == [0, 2]
    assert parse_selection("3,1,1", 3) == [0, 2]
    assert parse_selection("all", 3) == [0, 1, 2]
    assert parse_selection(" ALL ", 2) == [0, 1]
    assert parse_selection("none", 3) == []
    assert parse_selection("", 3) == []
    for bad in ("abc", "0", "4", "1,9", "-1"):
        assert parse_selection(bad, 3) is None


def test_ask_selection_reasks_until_valid() -> None:
    io = FakeIO("abc", "0", "4", "2")
    assert ask_selection(io, "Pick?", 3) == [1]
    assert io.prompts == ["Pick?"] * 4
