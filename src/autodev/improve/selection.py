from autodev.ports.user_io import UserIO


def parse_selection(text: str, count: int) -> list[int] | None:
    """0-based indexes, or None when the answer is invalid."""
    text = text.strip().lower()
    if text in ("", "none"):
        return []
    if text == "all":
        return list(range(count))
    chosen: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdecimal() or not 1 <= int(part) <= count:
            return None
        chosen.add(int(part) - 1)
    return sorted(chosen)


def ask_selection(io: UserIO, prompt: str, count: int) -> list[int]:
    selection = parse_selection(io.ask(prompt), count)
    while selection is None:
        selection = parse_selection(io.ask(prompt), count)
    return selection
