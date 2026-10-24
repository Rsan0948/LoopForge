"""Paid tier-2 compactor: validation, budget settlement and anchoring; no network."""

import json
from pathlib import Path

import httpx
import pytest

from loopforge.adapters.paid_budget import SQLitePaidBudget
from scripts.compaction_lab import GeminiCompactor, checkpoint_summarizer

KEY = "test-gemini-key-not-real-0123456789"
MESSAGES: list[dict[str, object]] = [
    {"role": "assistant", "content": "Plan", "tool_calls": []},
    {"role": "tool", "content": "edited app/x.py; check_project failed (assert 422 == 409)"},
]


def _handoff(evidence: str = "m1") -> dict[str, object]:
    note = {"text": "x.py edit applied; conflict test still fails", "evidence": [evidence]}
    return {
        "established_facts": [note],
        "changes_and_results": [],
        "failed_approaches": [],
        "unresolved_questions": [],
        "next_steps": [note],
    }


def _reply(
    text: str, *, finish: str = "STOP", prompt: int = 900, out: int = 120
) -> dict[str, object]:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": out,
            "totalTokenCount": prompt + out,
        },
    }


def _compactor(
    tmp_path: Path, reply: dict[str, object], status: int = 200
) -> tuple[GeminiCompactor, SQLitePaidBudget, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(status, json=reply)

    ledger = SQLitePaidBudget(tmp_path / "budget.sqlite", 2_000_000)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    return GeminiCompactor(ledger, tmp_path / "out", api_key=KEY, client=client), ledger, sent


def test_valid_handoff_is_returned_and_charged_from_reported_usage(tmp_path: Path) -> None:
    compactor, ledger, sent = _compactor(tmp_path, _reply(json.dumps(_handoff())))
    handoff = compactor.summarize(MESSAGES, "trial:1")
    assert handoff.next_steps[0].evidence == ["m1"]
    assert ledger.account().spent == 900 * 3 // 10 + 120 * 5 // 2
    assert sent[0]["generationConfig"]["responseMimeType"] == "application/json"  # type: ignore[index]


@pytest.mark.parametrize(
    "reply",
    [
        _reply("not json"),
        _reply(json.dumps({**_handoff(), "extra": 1})),
        _reply(json.dumps(_handoff(evidence="m99"))),
        _reply(json.dumps(_handoff()), finish="MAX_TOKENS"),
    ],
    ids=["invalid-json", "extra-field", "unknown-citation", "unfinished"],
)
def test_invalid_handoffs_are_refused_and_still_settled(
    tmp_path: Path, reply: dict[str, object]
) -> None:
    compactor, ledger, _ = _compactor(tmp_path, reply)
    with pytest.raises(ValueError):  # noqa: PT011 - every refusal is a validation error
        compactor.summarize(MESSAGES, "trial:1")
    account = ledger.account()
    assert account.spent > 0
    assert account.pending == ()


def test_http_failure_charges_the_full_reservation(tmp_path: Path) -> None:
    compactor, ledger, _ = _compactor(tmp_path, {"error": "x"}, status=503)
    with pytest.raises(ValueError, match="HTTP 503"):
        compactor.summarize(MESSAGES, "trial:1")
    assert ledger.account().pending == ()
    assert ledger.account().spent > 900 * 3 // 10 + 120 * 5 // 2


def test_credential_in_the_evidence_is_never_sent(tmp_path: Path) -> None:
    compactor, _, sent = _compactor(tmp_path, _reply(json.dumps(_handoff())))
    leaked: list[dict[str, object]] = [{"role": "tool", "content": f"env: GEMINI_API_KEY={KEY}"}]
    with pytest.raises(ValueError, match="admission"):
        compactor.summarize(leaked, "trial:1")
    assert sent == []


def test_summarizer_hook_anchors_on_the_previous_handoff(tmp_path: Path) -> None:
    compactor, _, sent = _compactor(tmp_path, _reply(json.dumps(_handoff("m0"))))
    hook = checkpoint_summarizer(compactor, "trial")
    first = hook(MESSAGES, None)
    hook(MESSAGES, first)
    first_evidence = json.loads(sent[0]["contents"][0]["parts"][0]["text"])  # type: ignore[index]
    second_evidence = json.loads(sent[1]["contents"][0]["parts"][0]["text"])  # type: ignore[index]
    assert [r["message"]["role"] for r in first_evidence] == ["assistant", "tool"]
    assert second_evidence[0]["message"] == {"role": "previous_handoff", "content": first}


def test_provider_schema_drops_item_counts_but_local_validation_keeps_them(tmp_path: Path) -> None:
    long_note = {"text": "x" * 1601, "evidence": ["m1"]}
    reply = _reply(json.dumps({**_handoff(), "next_steps": [long_note]}))
    compactor, _, sent = _compactor(tmp_path, reply)
    with pytest.raises(ValueError):  # noqa: PT011 - over-long note refused locally
        compactor.summarize(MESSAGES, "trial:1")
    schema = json.dumps(sent[0]["generationConfig"]["responseJsonSchema"])  # type: ignore[index]
    assert "maxItems" not in schema
    assert "maxLength" in schema
    assert '"additionalProperties": false' in schema


def test_evidence_values_are_constrained_to_this_requests_message_ids(tmp_path: Path) -> None:
    compactor, _, sent = _compactor(tmp_path, _reply(json.dumps(_handoff())))
    compactor.summarize(MESSAGES, "trial:1")
    schema = sent[0]["generationConfig"]["responseJsonSchema"]  # type: ignore[index]
    evidence = schema["$defs"]["Note"]["properties"]["evidence"]  # type: ignore[index]
    assert evidence["items"] == {"type": "string", "enum": ["m0", "m1"]}
