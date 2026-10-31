import pytest

from loopforge.adapters.exact_text import TextReplacement, replace_exact_text


def test_replacements_are_sequential_and_preserve_unicode_and_newlines() -> None:
    assert (
        replace_exact_text(
            "é\r\nx", (TextReplacement("é", "雪"), TextReplacement("雪\r\nx", "界\r\n"))
        )
        == "界\r\n"
    )


@pytest.mark.parametrize(("source", "old", "count"), [("x", "y", 0), ("xx", "x", 2)])
def test_failure_identifies_index_and_count_without_leaking_text(
    source: str, old: str, count: int
) -> None:
    with pytest.raises(ValueError, match=f"replacement 2: old text matched {count} times"):
        replace_exact_text(
            "secret" + source, (TextReplacement("secret", ""), TextReplacement(old, "z"))
        )


def test_empty_match_is_denied() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        replace_exact_text("abc", (TextReplacement("", "bad"),))
