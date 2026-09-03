"""Pins for the M4 trajectory-quality metrics (PACS-016).

Every metric gets synthetic-stream allow+deny-style pins (AGENTS.md rule 10):
the empty stream yields all zeros; repetition pins cover exact duplicates at
known ratios AND same-tool-different-args NOT counting; expensive-turn pins
cover provider/model mismatches; scope pins cover out-of-scope vs in-scope
paths, non-path argument values (search patterns) NOT counting, empty
prefixes, and invalid path values; context-token pins cover BOTH directions
of the two-source maximum contract; recovery and human-intervention pins
cover the full event mixes. All streams are hand-built durable event tuples;
no network, no sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from loopforge.application.trajectory import (
    ProposalArgumentsContractError,
    compute_trajectory_metrics,
    count_human_interventions,
)
from loopforge.domain.actions import ActionProposal
from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkSandboxMode,
    BenchmarkTaskSpec,
    GraderId,
)
from loopforge.domain.context_lifecycle import (
    AccountingEntry,
    ContextAccounting,
    ContextTokenBudget,
    DropReason,
)
from loopforge.domain.events import (
    ActionProposed,
    ApprovalGranted,
    ApprovalRejected,
    ApprovalRequested,
    BudgetDebited,
    CircuitOpened,
    Event,
    ModelTurnRecorded,
    OperatorInstruction,
    ReflectionRecorded,
    RetryScheduled,
    RunStarted,
    RunStopped,
    ToolFailed,
    ToolSucceeded,
    VerificationPassed,
)
from loopforge.domain.reliability import ToolFailureClass
from loopforge.domain.types import (
    ActionId,
    ContextItemId,
    EventId,
    RunId,
    StopReason,
    UsageDelta,
)

NOW = datetime(2026, 9, 3, tzinfo=UTC)
RUN = RunId("trajectory-run")


def _event_id(sequence: int) -> EventId:
    return EventId(f"e{sequence}")


def _spec(allowed_prefixes: tuple[str, ...] = ("src",)) -> BenchmarkTaskSpec:
    return BenchmarkTaskSpec(
        task_id="bench-trajectory",
        category=BenchmarkCategory.SIMPLE_BUG,
        objective="repair the fixture so the tests pass",
        fixture_id="bench-trajectory",
        sandbox_mode=BenchmarkSandboxMode.CONTAINER,
        grader_ids=(GraderId.VERIFIED_SUCCESS,),
        allowed_prefixes=allowed_prefixes,
    )


def _base(sequence: int) -> dict[str, object]:
    return {
        "event_id": _event_id(sequence),
        "run_id": RUN,
        "occurred_at": NOW,
        "sequence": sequence,
    }


def _turn(sequence: int, provider: str = "stub", model: str = "cheap-1") -> ModelTurnRecorded:
    return ModelTurnRecorded(
        **_base(sequence),  # type: ignore[arg-type]
        provider=provider,
        model=model,
        action_id=ActionId(f"a{sequence}"),
    )


def _proposed(
    sequence: int, tool_name: str, arguments: dict[str, str], action_id: str | None = None
) -> ActionProposed:
    return ActionProposed(
        **_base(sequence),  # type: ignore[arg-type]
        proposal=ActionProposal(
            action_id=ActionId(action_id or f"a{sequence}"),
            tool_name=tool_name,
            arguments=arguments,
        ),
    )


def _debit(sequence: int, input_tokens: int) -> BudgetDebited:
    return BudgetDebited(
        **_base(sequence),  # type: ignore[arg-type]
        usage=UsageDelta(cost_usd=0.01, input_tokens=input_tokens, output_tokens=20),
    )


def _accounting(  # noqa: PLR0913 - test ledger builder keeps every knob explicit
    *,
    used_content: int,
    overhead: int = 0,
    dropped: int = 0,
    compacted_kept: int = 0,
    max_tokens: int = 4096,
    reserve: int = 256,
) -> ContextAccounting:
    entries: list[AccountingEntry] = []
    if used_content:
        entries.append(
            AccountingEntry(item_id=ContextItemId("kept-1"), tokens=used_content, kept=True)
        )
    entries.extend(
        AccountingEntry(
            item_id=ContextItemId(f"compacted-{index}"),
            tokens=10,
            kept=True,
            compacted=True,
        )
        for index in range(compacted_kept)
    )
    entries.extend(
        AccountingEntry(
            item_id=ContextItemId(f"dropped-{index}"),
            tokens=0,
            kept=False,
            drop_reason=DropReason.OVER_BUDGET,
        )
        for index in range(dropped)
    )
    return ContextAccounting(
        budget=ContextTokenBudget(max_tokens=max_tokens, reserve_tokens=reserve),
        entries=tuple(entries),
        overhead_tokens=overhead,
    )


# --- Empty stream: every metric is honestly zero ---------------------------------


def test_empty_stream_yields_all_zeros() -> None:
    metrics = compute_trajectory_metrics(_spec(), ())

    assert metrics.model_turns == 0
    assert metrics.repetition_ratio == 0.0
    assert metrics.expensive_model_turns == 0
    assert metrics.scope_violations == 0
    assert metrics.permission_requests == 0
    assert metrics.context_tokens_used == 0
    assert metrics.context_items_dropped == 0
    assert metrics.recovery_events == 0
    assert count_human_interventions(()) == 0


# --- model_turns -----------------------------------------------------------------


def test_model_turns_counts_model_turn_recorded_only() -> None:
    events: tuple[Event, ...] = (
        RunStarted(**_base(1), objective="repair"),  # type: ignore[arg-type]
        _turn(2),
        _turn(3),
        _proposed(4, "write_file", {"path": "src/a.py", "content": "x"}),
        VerificationPassed(**_base(5), summary="ok"),  # type: ignore[arg-type]
        RunStopped(**_base(6), reason=StopReason.SUCCESS_VERIFIED, summary="done"),  # type: ignore[arg-type]
    )
    assert compute_trajectory_metrics(_spec(), events).model_turns == 2


# --- repetition_ratio ------------------------------------------------------------


def test_repetition_ratio_is_zero_without_proposals() -> None:
    events: tuple[Event, ...] = (_turn(1), _turn(2))
    assert compute_trajectory_metrics(_spec(), events).repetition_ratio == 0.0


def test_repetition_ratio_counts_exact_duplicates_only() -> None:
    # 4 proposals: positions 3 and 4 duplicate earlier signatures -> 2/4.
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "src/a.py", "content": "x"}, "p1"),
        _proposed(2, "read_file", {"path": "src/a.py"}, "p2"),
        _proposed(3, "write_file", {"path": "src/a.py", "content": "x"}, "p3"),
        _proposed(4, "read_file", {"path": "src/a.py"}, "p4"),
    )
    assert compute_trajectory_metrics(_spec(), events).repetition_ratio == 0.5


def test_repetition_ratio_ignores_same_tool_different_arguments() -> None:
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "src/a.py", "content": "x"}, "p1"),
        _proposed(2, "write_file", {"path": "src/a.py", "content": "y"}, "p2"),
        _proposed(3, "write_file", {"path": "src/b.py", "content": "x"}, "p3"),
    )
    assert compute_trajectory_metrics(_spec(), events).repetition_ratio == 0.0


def test_repetition_ratio_treats_argument_order_as_irrelevant() -> None:
    events: tuple[Event, ...] = (
        _proposed(1, "edit_file", {"path": "src/a.py", "old": "x", "new": "y"}, "p1"),
        _proposed(2, "edit_file", {"new": "y", "old": "x", "path": "src/a.py"}, "p2"),
    )
    assert compute_trajectory_metrics(_spec(), events).repetition_ratio == 0.5


def test_repetition_ratio_full_stall_is_one_minus_first() -> None:
    # A 4-turn stall repeating one identical proposal: 3 duplicates of 4.
    events: tuple[Event, ...] = tuple(
        _proposed(index, "write_file", {"path": "src/a.py", "content": "x"}, f"p{index}")
        for index in range(1, 5)
    )
    assert compute_trajectory_metrics(_spec(), events).repetition_ratio == 0.75


def test_non_serializable_proposal_arguments_fail_loudly() -> None:
    # ActionProposal.arguments is typed Mapping[str, str] but not validated
    # per value; a misbehaving caller can persist arguments json.dumps cannot
    # canonicalize. The projection raises its code-owned contract error
    # instead of leaking a bare TypeError.
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "src/a.py", "content": b"bytes"}),  # pyright: ignore[reportArgumentType]
    )
    with pytest.raises(ProposalArgumentsContractError, match="not JSON-serializable"):
        compute_trajectory_metrics(_spec(), events)


# --- expensive_model_turns -------------------------------------------------------


def test_expensive_turns_match_exact_provider_model_pairs() -> None:
    expensive = frozenset({("stub", "big-1")})
    events: tuple[Event, ...] = (
        _turn(1, provider="stub", model="big-1"),
        _turn(2, provider="stub", model="big-1"),
        _turn(3, provider="stub", model="cheap-1"),
    )
    metrics = compute_trajectory_metrics(_spec(), events, expensive_models=expensive)
    assert metrics.model_turns == 3
    assert metrics.expensive_model_turns == 2


def test_expensive_turns_reject_provider_or_model_mismatch() -> None:
    expensive = frozenset({("stub", "big-1")})
    events: tuple[Event, ...] = (
        _turn(1, provider="other", model="big-1"),  # same model, wrong provider
        _turn(2, provider="stub", model="big-2"),  # same provider, wrong model
        _turn(3, provider="stub", model="big-1"),
    )
    metrics = compute_trajectory_metrics(_spec(), events, expensive_models=expensive)
    assert metrics.expensive_model_turns == 1


def test_expensive_turns_default_empty_set() -> None:
    events: tuple[Event, ...] = (_turn(1), _turn(2))
    assert compute_trajectory_metrics(_spec(), events).expensive_model_turns == 0


# --- scope_violations ------------------------------------------------------------


def test_scope_violations_count_out_of_scope_mutating_proposals() -> None:
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "src/a.py", "content": "x"}),  # in scope
        _proposed(2, "write_file", {"path": "secrets/key.pem", "content": "x"}),  # out
        _proposed(3, "edit_file", {"path": "etc/config", "old": "a", "new": "b"}),  # out
        _proposed(4, "revert_file", {"path": "src/b.py"}),  # in scope
    )
    assert compute_trajectory_metrics(_spec(), events).scope_violations == 2


def test_scope_violations_prefix_boundary_semantics() -> None:
    # "srcfile.py" merely starts with the "src" characters; it is neither the
    # prefix itself nor a path under it. "src/deep/nested.py" lives under it.
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "srcfile.py", "content": "x"}),  # out
        _proposed(2, "write_file", {"path": "src/deep/nested.py", "content": "x"}),  # in
    )
    assert compute_trajectory_metrics(_spec(), events).scope_violations == 1


def test_scope_violations_ignore_non_path_argument_values() -> None:
    # search_files carries a "query" (a search pattern, not a path): even a
    # pattern that LOOKS like an out-of-scope path is not a scope violation.
    events: tuple[Event, ...] = (
        _proposed(1, "search_files", {"query": "secrets/key.pem"}),
        _proposed(2, "read_file", {"path": "secrets/key.pem"}),  # read-only tool
    )
    assert compute_trajectory_metrics(_spec(), events).scope_violations == 0


def test_scope_violations_empty_prefixes_allow_all() -> None:
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "anywhere/at/all.py", "content": "x"}),
    )
    assert compute_trajectory_metrics(_spec(()), events).scope_violations == 0


def test_scope_violations_ignore_invalid_path_values() -> None:
    # Absolute paths, traversal, and empty segments are rejected at the
    # schema/tool boundary; this metric measures discipline, not malformation.
    events: tuple[Event, ...] = (
        _proposed(1, "write_file", {"path": "/etc/passwd", "content": "x"}),
        _proposed(2, "write_file", {"path": "../escape.py", "content": "x"}),
        _proposed(3, "write_file", {"path": "src//double.py", "content": "x"}),
        _proposed(4, "write_file", {"content": "x"}),  # path argument missing
    )
    assert compute_trajectory_metrics(_spec(), events).scope_violations == 0


def test_scope_violations_path_arguments_override() -> None:
    events: tuple[Event, ...] = (
        _proposed(1, "deploy_service", {"target_dir": "ops/prod", "version": "1"}),
        _proposed(2, "write_file", {"path": "secrets/key.pem", "content": "x"}),
    )
    metrics = compute_trajectory_metrics(
        _spec(),
        events,
        path_arguments={"deploy_service": "target_dir"},
    )
    # Only the overridden mapping applies: deploy_service is judged,
    # write_file is no longer recognized as file-mutating by this caller.
    assert metrics.scope_violations == 1


# --- permission_requests ---------------------------------------------------------


def test_permission_requests_count_approval_requested_only() -> None:
    events: tuple[Event, ...] = (
        ApprovalRequested(**_base(1), action_id=ActionId("a1"), reason="risky"),  # type: ignore[arg-type]
        ApprovalGranted(**_base(2), action_id=ActionId("a1")),  # type: ignore[arg-type]
        ApprovalRequested(**_base(3), action_id=ActionId("a2"), reason="risky again"),  # type: ignore[arg-type]
        ApprovalRejected(**_base(4), action_id=ActionId("a2"), reason="no"),  # type: ignore[arg-type]
    )
    assert compute_trajectory_metrics(_spec(), events).permission_requests == 2


# --- context_tokens_used two-source maximum --------------------------------------


def test_context_tokens_used_never_under_reports_a_higher_billed_peak() -> None:
    events: tuple[Event, ...] = (_debit(1, 900), _debit(2, 1200))
    accountings = (
        _accounting(used_content=300, overhead=50),
        _accounting(used_content=500, overhead=50),
    )
    metrics = compute_trajectory_metrics(_spec(), events, context_accountings=accountings)
    # The billed peak (1200) exceeds the accounting peak (550): the metric is
    # the MAXIMUM of the two sources, so a higher real billed prompt size is
    # never silently under-reported by a partial ledger.
    assert metrics.context_tokens_used == 1200


def test_context_tokens_used_uses_the_accounting_peak_when_it_is_higher() -> None:
    events: tuple[Event, ...] = (_debit(1, 900), _debit(2, 1200))
    accountings = (
        _accounting(used_content=1500, overhead=50),
        _accounting(used_content=500, overhead=50),
    )
    metrics = compute_trajectory_metrics(_spec(), events, context_accountings=accountings)
    # The accounting peak (1550) exceeds the billed peak (1200): the runtime's
    # budgeting ledger observed a larger assembly than any single billed turn.
    assert metrics.context_tokens_used == 1550


def test_context_tokens_used_falls_back_to_peak_billed_input_tokens() -> None:
    events: tuple[Event, ...] = (_debit(1, 900), _debit(2, 1200), _debit(3, 700))
    metrics = compute_trajectory_metrics(_spec(), events)
    assert metrics.context_tokens_used == 1200


# --- context_items_dropped --------------------------------------------------------


def test_context_items_dropped_sums_dropped_and_compacted() -> None:
    accountings = (
        _accounting(used_content=100, dropped=2, compacted_kept=1),
        _accounting(used_content=100, dropped=1, compacted_kept=2),
    )
    metrics = compute_trajectory_metrics(_spec(), (), context_accountings=accountings)
    assert metrics.context_items_dropped == 6


def test_context_items_dropped_is_zero_without_accountings() -> None:
    # The durable stream does not carry compaction detail: honestly 0.
    events: tuple[Event, ...] = (_turn(1), _debit(2, 500))
    assert compute_trajectory_metrics(_spec(), events).context_items_dropped == 0


# --- recovery_events --------------------------------------------------------------


def test_recovery_events_count_the_full_mix() -> None:
    events: tuple[Event, ...] = (
        ToolFailed(
            **_base(1),  # type: ignore[arg-type]
            action_id=ActionId("a1"),
            error_code="IO",
            error_message="disk",
            failure_class=ToolFailureClass.TRANSIENT,
        ),
        RetryScheduled(
            **_base(2),  # type: ignore[arg-type]
            action_id=ActionId("a1"),
            next_attempt=2,
            delay_seconds=0.5,
            reason_code="transient",
        ),
        ToolFailed(
            **_base(3),  # type: ignore[arg-type]
            action_id=ActionId("a1"),
            error_code="IO",
            error_message="disk again",
            failure_class=ToolFailureClass.PERMANENT,
        ),
        CircuitOpened(**_base(4), tool_name="write_file", reason_code="streak"),  # type: ignore[arg-type]
        ReflectionRecorded(**_base(5), reflection="the write keeps failing"),  # type: ignore[arg-type]
        # Non-recovery events must not leak into the count.
        ToolSucceeded(
            **_base(6),  # type: ignore[arg-type]
            action_id=ActionId("a2"),
            observation="ok",
            attempt=1,
        ),
        VerificationPassed(**_base(7), summary="ok"),  # type: ignore[arg-type]
    )
    assert compute_trajectory_metrics(_spec(), events).recovery_events == 5


# --- count_human_interventions ----------------------------------------------------


def test_human_interventions_count_grants_rejections_and_instructions() -> None:
    events: tuple[Event, ...] = (
        ApprovalRequested(**_base(1), action_id=ActionId("a1"), reason="risky"),  # type: ignore[arg-type]
        ApprovalGranted(**_base(2), action_id=ActionId("a1")),  # type: ignore[arg-type]
        ApprovalRejected(**_base(3), action_id=ActionId("a2"), reason="denied"),  # type: ignore[arg-type]
        OperatorInstruction(**_base(4), instruction="focus on the parser"),  # type: ignore[arg-type]
        OperatorInstruction(  # type: ignore[arg-type]
            **_base(5),  # type: ignore[arg-type]
            instruction="new objective",
            amends_objective=True,
        ),
    )
    # The request itself is the harness asking, not a human intervening.
    assert count_human_interventions(events) == 4
