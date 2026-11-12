"""Validate sequential exact replacements before the caller performs one write."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextReplacement:
    old: str
    new: str


def replace_exact_text(source: str, edits: tuple[TextReplacement, ...]) -> str:
    """Return new text, or refuse the whole batch without exposing source in errors.

    Each match is measured after earlier replacements, preserving sequential edits.
    File hashes, size limits, write admission and atomic persistence belong to callers.
    """
    result = source
    for index, edit in enumerate(edits, start=1):
        if not edit.old:
            msg = f"replacement {index}: old text must be nonempty"
            raise ValueError(msg)
        first = result.find(edit.old)
        # A second occurrence may overlap the first; str.count skips those matches.
        matches = 0 if first < 0 else 2 if result.find(edit.old, first + 1) >= 0 else 1
        if matches != 1:
            msg = (
                f"replacement {index}: old text matched {matches} times; each edit target "
                "must be unique. Select exact surrounding source from the current file; "
                "no edits applied"
            )
            raise ValueError(msg)
        result = result.replace(edit.old, edit.new, 1)
    return result
