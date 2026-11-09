from autodev.pipeline.commit_message import commit_message


def test_short_title_fits_in_subject() -> None:
    assert commit_message("feat", "T1", "Add cart") == "feat(T1): Add cart"


def test_long_title_cut_at_word_boundary_with_full_title_in_body() -> None:
    title = "Register the payment of an order when the customer confirms the cart contents today"
    message = commit_message("test", "T2", title, limit=40)
    subject, blank, body = message.split("\n", 2)
    assert subject == "test(T2): Register the payment of an…"
    assert blank == ""
    assert body == title


def test_subject_never_exceeds_limit() -> None:
    titles = ["word " * 40, "x" * 200, "ä" * 100 + " tail", "a b c d e f g h"]
    for title in titles:
        for limit in (20, 40, 72):
            subject = commit_message("feat", "T10", title, limit=limit).split("\n")[0]
            assert len(subject) <= limit
            assert subject.startswith("feat(T10): ")
