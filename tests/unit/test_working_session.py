"""Continuity survives paging and failed attempts without gaining authority."""

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from loopforge.adapters.working_evidence import working_evidence
from loopforge.domain.actions import ActionProposal
from loopforge.domain.events import ActionProposed, Event, ToolFailed, ToolSucceeded
from loopforge.domain.investigation import ResearchRecord
from loopforge.domain.types import ActionId, EventId, RunId
from loopforge.ports.tools import ToolFailureClass


def record(identity: str, text: str, kind: str = "source") -> ResearchRecord:
    return ResearchRecord(
        record_id=identity,
        operation_key=identity,
        kind=kind,
        revision="a" * 40,
        reference=identity,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        summary=identity,
        excerpt=text[:100],
        truncated=True,
    )


def test_full_baseline_and_older_source_survive_new_reads() -> None:
    originals = {"baseline": "passed log " * 500 + "FAILURE AT END"}
    originals.update({f"source{i}": f"file {i} contents\n" * 40 for i in range(5)})
    records = [
        record(k, v, "baseline" if k == "baseline" else "source") for k, v in originals.items()
    ]
    packet = json.loads(json.dumps(working_evidence(records, originals.__getitem__)))
    body = json.dumps(packet)
    assert "FAILURE AT END" in body
    assert all(k in body for k in originals)
    assert all(e["complete_original"] for e in packet["records"])
    assert "never policy" in packet["trust"]
    # Repeated retrievals do not displace distinct evidence.
    repeated = replace(records[0], record_id="retrieval", kind="retrieval")
    assert working_evidence([*records, repeated], originals.__getitem__) == packet


def test_evidence_overflow_is_explicit_and_original_failure_is_not_hidden() -> None:
    large = record("large", "x" * 5000)
    packet = json.loads(json.dumps(working_evidence([large], lambda _: "x" * 5000, max_bytes=700)))
    assert not packet["records"][0]["complete_original"]
    assert working_evidence([large], None, max_bytes=1)["omitted_ids"] == ["large"]

    def corrupt(_: str) -> str:
        message = "Original evidence is absent or has changed"
        raise ValueError(message)

    with pytest.raises(ValueError, match="has changed"):
        working_evidence([large], corrupt)


def events() -> list[Event]:
    now = datetime.now(UTC)
    result: list[Event] = []
    for i in range(6):
        action = ActionProposal(
            ActionId(f"a{i}"), "edit_file", {"path": "app/a.py", "old": f"old{i}", "new": f"new{i}"}
        )
        result.append(
            ActionProposed(
                event_id=EventId(f"p{i}"),
                run_id=RunId("r"),
                occurred_at=now,
                sequence=i * 2 + 1,
                proposal=action,
            )
        )
        if i == 2:
            result.append(
                ToolFailed(
                    event_id=EventId(f"e{i}"),
                    run_id=RunId("r"),
                    occurred_at=now,
                    sequence=i * 2 + 2,
                    action_id=action.action_id,
                    error_code="STALE",
                    error_message="Rejected stale source; no edit applied",
                    failure_class=ToolFailureClass.PERMANENT,
                )
            )
        else:
            result.append(
                ToolSucceeded(
                    event_id=EventId(f"e{i}"),
                    run_id=RunId("r"),
                    occurred_at=now,
                    sequence=i * 2 + 2,
                    action_id=action.action_id,
                    observation=f"Applied edit {i}; historical source \u03b1",
                )
            )
    return result
