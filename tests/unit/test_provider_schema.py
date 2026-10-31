import pytest

from loopforge.adapters.provider_schema import enum_from_const


def test_single_literal_is_preserved_without_mutating_original_schema() -> None:
    original = {"type": "string", "const": "task_objective"}
    assert enum_from_const(original) == {"type": "string", "enum": ["task_objective"]}
    assert original["const"] == "task_objective"


def test_schema_without_const_is_unchanged() -> None:
    assert enum_from_const({"enum": ["a", "b"]}) == {"enum": ["a", "b"]}


def test_contradictory_schema_is_rejected_instead_of_widened() -> None:
    with pytest.raises(ValueError, match="Conflicting"):
        enum_from_const({"const": "a", "enum": ["b"]})
