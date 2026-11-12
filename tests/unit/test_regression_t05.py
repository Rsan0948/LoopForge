from loopforge.adapters.exact_text import TextReplacement, replace_exact_text


def test_overlapping_exact_edit_targets_are_ambiguous() -> None:
    source = 'value = "ababa"\n'
    try:
        result = replace_exact_text(source, (TextReplacement("aba", "fixed"),))
    except ValueError:
        return
    assert result == source, result
