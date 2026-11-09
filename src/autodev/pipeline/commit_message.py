_ELLIPSIS = "…"


def commit_message(kind: str, scope: str, title: str, limit: int = 72) -> str:
    """Conventional commit message; a long title is cut in the subject, kept whole in the body."""
    prefix = f"{kind}({scope}): "
    subject = f"{prefix}{title}"
    if len(subject) <= limit:
        return subject
    room = limit - len(prefix) - len(_ELLIPSIS)
    cut = ""
    for word in title.split():
        candidate = f"{cut} {word}" if cut else word
        if len(candidate) > room:
            break
        cut = candidate
    if not cut:
        cut = title[: max(room, 0)].rstrip()
    return f"{prefix}{cut}{_ELLIPSIS}\n\n{title}"
