from __future__ import annotations

import dataclasses

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from loopforge.domain.actions import ActionProposal
from loopforge.domain.types import ActionId


def test_action_proposal_carries_model_intent() -> None:
    proposal = ActionProposal(
        action_id=ActionId("a1"),
        tool_name="inspect",
        arguments={"path": "src/loopforge"},
        expected_observation="all tests pass",
    )
    assert proposal.action_id == "a1"
    assert proposal.tool_name == "inspect"
    assert proposal.arguments == {"path": "src/loopforge"}
    assert proposal.expected_observation == "all tests pass"


def test_action_proposal_expected_observation_defaults_to_none() -> None:
    proposal = ActionProposal(
        action_id=ActionId("a1"),
        tool_name="inspect",
        arguments={},
    )
    assert proposal.expected_observation is None


def test_action_proposal_accepts_tool_name_with_surrounding_whitespace() -> None:
    proposal = ActionProposal(
        action_id=ActionId("a1"),
        tool_name=" inspect ",
        arguments={},
    )
    assert proposal.tool_name == " inspect "


@pytest.mark.parametrize("tool_name", ["", " ", "\t\n"])
def test_action_proposal_rejects_blank_tool_name(tool_name: str) -> None:
    with pytest.raises(ValueError, match="tool_name cannot be empty"):
        ActionProposal(
            action_id=ActionId("a1"),
            tool_name=tool_name,
            arguments={},
        )


@given(tool_name=st.text(min_size=1).filter(lambda name: name.strip()))
@example(tool_name="inspect")
def test_action_proposal_accepts_any_non_blank_tool_name(tool_name: str) -> None:
    proposal = ActionProposal(
        action_id=ActionId("a1"),
        tool_name=tool_name,
        arguments={},
    )
    assert proposal.tool_name == tool_name


@given(tool_name=st.text(alphabet=st.sampled_from([" ", "\t", "\n", "\r", "\x0b", "\x0c"])))
@example(tool_name="")
@example(tool_name=" ")
def test_action_proposal_rejects_any_whitespace_only_tool_name(tool_name: str) -> None:
    with pytest.raises(ValueError, match="tool_name cannot be empty"):
        ActionProposal(
            action_id=ActionId("a1"),
            tool_name=tool_name,
            arguments={},
        )


def test_action_proposal_is_frozen() -> None:
    proposal = ActionProposal(
        action_id=ActionId("a1"),
        tool_name="inspect",
        arguments={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.tool_name = "other"  # type: ignore[misc]


def test_action_proposal_equality_compares_all_fields() -> None:
    base = ActionProposal(ActionId("a1"), "inspect", {"path": "src"}, "done")
    assert base == ActionProposal(ActionId("a1"), "inspect", {"path": "src"}, "done")
    assert base != ActionProposal(ActionId("a2"), "inspect", {"path": "src"}, "done")
    assert base != ActionProposal(ActionId("a1"), "inspect", {"path": "src"})


def test_action_proposal_does_not_carry_policy_fields() -> None:
    # Rule 4: risk, permission, retry, idempotency, approval, and timeout
    # policy belong to the registered tool contract, never to model output.
    fields = {field.name for field in dataclasses.fields(ActionProposal)}
    assert fields == {"action_id", "tool_name", "arguments", "expected_observation"}
