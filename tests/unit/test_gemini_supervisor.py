"""Hermetic Gemini transport fixtures; no live calls or real credentials."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

import loopforge.adapters.gemini_supervisor as gemini_module
from loopforge.adapters.gemini_supervisor import (
    MAX_OUTPUT_TOKENS,
    DecisionPayload,
    GeminiSupervisor,
    load_gemini_key,
)
from loopforge.adapters.goal_journal import SQLiteGoalJournal
from loopforge.adapters.research_store import ResearchStore
from loopforge.domain.goals import GoalAction, GoalPolicy, GoalState, WorkerObservation
from loopforge.domain.investigation import InspectionRequest, ResearchRecord
from loopforge.ports.supervisor import SupervisorFailure


@pytest.fixture(autouse=True)
def fixture_pricing_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2100, 1, 1, tzinfo=UTC))


KEY = "fixture-credential-do-not-emit"


def state() -> GoalState:
    return GoalState(
        policy=GoalPolicy(
            "Fix synthetic arithmetic",
            "a" * 64,
            5000000,
            9999999999,
            remote_data="synthetic-fixture",
        ),
        phase="ready",
    )


def response(
    *, action: str = "delegate", usage: bool = True, finish: str = "STOP"
) -> dict[str, object]:
    result: dict[str, object] = {
        "modelVersion": "gemini-4-argon-fixture",
        "candidates": [
            {
                "finishReason": finish,
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "action": action,
                                    "rationale": "Inspect prepared evidence",
                                    "plan": "Run local worker then review",
                                }
                            ),
                            "thoughtSignature": "opaque-signature",
                        }
                    ],
                },
            }
        ],
    }
    if usage:
        result["usageMetadata"] = {
            "promptTokenCount": 100,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 20,
            "cachedContentTokenCount": 50,
            "totalTokenCount": 150,
        }
    return result


def test_strict_decision_and_billed_thinking_with_metadata_preserved() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response())

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        prepared = adapter.prepare(state())
        result = adapter.decide(prepared)
    assert result.decision.action == GoalAction.DELEGATE
    assert result.charged_microusd == 700  # cached input billed at full rate conservatively
    assert "opaque-signature" in result.trace
    assert "thoughtsTokenCount" in result.trace
    assert KEY not in prepared.body + prepared.trace + result.trace
    assert requests[0].headers["x-goog-api-key"] == KEY
    assert "key=" not in str(requests[0].url)
    body = json.loads(requests[0].content)
    assert body["generationConfig"]["maxOutputTokens"] == MAX_OUTPUT_TOKENS
    assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
    bound = len(prepared.body.encode()) + 4096
    assert prepared.maximum_microusd == bound * 2 + MAX_OUTPUT_TOKENS * 10


@pytest.mark.parametrize(
    "payload",
    [
        response(action="merge"),
        response(finish="MAX_TOKENS"),
        {"candidates": []},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": (
                                    '{"action":"delegate","rationale":"ok",'
                                    '"plan":"ok","permission":"root"}'
                                )
                            }
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        },
    ],
)
def test_invalid_or_truncated_output_never_executes(payload: dict[str, object]) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure):
            adapter.decide(adapter.prepare(state()))


def test_missing_usage_charges_whole_reservation() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(usage=False)))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        request = adapter.prepare(state())
        assert adapter.decide(request).charged_microusd == request.maximum_microusd


@pytest.mark.parametrize("status", [400, 429, 500])
def test_http_errors_do_not_echo_provider_body_or_retry(status: int) -> None:
    calls = 0

    def transport(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=KEY + " private server detail")

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure) as caught:
            adapter.decide(adapter.prepare(state()))
    assert KEY not in str(caught.value)
    assert caught.value.charged_microusd is None
    assert calls == 1


def test_timeout_is_unknown_and_not_retried() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(KEY, request=request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure, match="unknown") as caught:
            adapter.decide(adapter.prepare(state()))
    assert KEY not in str(caught.value)


def test_remote_scope_and_credential_exclusion_fail_before_network() -> None:
    adapter = GeminiSupervisor(KEY)
    try:
        original = state()
        assert original.policy is not None
        with pytest.raises(SupervisorFailure, match="sharing"):
            adapter.prepare(replace(original, policy=replace(original.policy, remote_data="none")))
        with pytest.raises(SupervisorFailure, match="exclusion"):
            adapter.prepare(replace(original, observation=WorkerObservation(evidence=KEY)))
        with pytest.raises(SupervisorFailure, match="admission"):
            adapter.prepare(replace(original, observation=WorkerObservation(evidence="x" * 100000)))
    finally:
        adapter.close()


def test_usage_must_fit_reserved_bounds() -> None:
    payload = response()
    payload["usageMetadata"] = {
        "promptTokenCount": 100,
        "candidatesTokenCount": MAX_OUTPUT_TOKENS,
        "thoughtsTokenCount": 1,
        "totalTokenCount": 100 + MAX_OUTPUT_TOKENS + 1,
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure, match="bounds") as caught:
            adapter.decide(adapter.prepare(state()))
        assert caught.value.charged_microusd is None


def test_key_loader_reads_only_named_key_without_shell_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text("OTHER=private\nGEMINI_API_KEY='fixture-token'\n")
    assert load_gemini_key(path) == "fixture-token"
    monkeypatch.setenv("GEMINI_API_KEY", "from-environment")
    assert load_gemini_key() == "from-environment"
    path.write_text("OTHER=private\n")
    with pytest.raises(ValueError, match="missing"):
        load_gemini_key(path)


def test_expired_price_lock_denies_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_module, "PRICE_VALID_UNTIL", datetime(2000, 1, 1, tzinfo=UTC))
    adapter = GeminiSupervisor(KEY)
    try:
        with pytest.raises(SupervisorFailure, match="pricing"):
            adapter.prepare(state())
    finally:
        adapter.close()


def test_oversized_response_denies_before_decoding() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 1000001))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure, match="bounded envelope"):
            adapter.decide(adapter.prepare(state()))


def test_inspection_schema_advertises_domain_bounds_and_preserves_domain_type() -> None:

    schema = DecisionPayload.model_json_schema()
    inspection = schema["properties"]["inspection"]["anyOf"][0]
    assert inspection["properties"]["line_count"]["maximum"] == 120
    assert inspection["properties"]["start_line"]["minimum"] == 1
    parsed = DecisionPayload.model_validate_json(
        json.dumps(
            {
                "action": "inspect",
                "rationale": "Read source",
                "plan": "Inspect the bounded source page",
                "inspection": {"path": "app/game.js", "line_count": 120},
            }
        )
    )
    assert isinstance(parsed.inspection, InspectionRequest)
    with pytest.raises(ValueError, match="outside supported bounds"):
        DecisionPayload.model_validate_json(
            json.dumps(
                {
                    "action": "inspect",
                    "rationale": "Read source",
                    "plan": "Inspect source",
                    "inspection": {"path": "app/game.js", "line_count": 121},
                }
            )
        )


def test_safe_failure_feedback_is_included_without_provider_values() -> None:
    adapter = GeminiSupervisor(KEY)
    try:
        request = adapter.prepare(replace(state(), summary="inspection: value_error"))
        context = json.loads(json.loads(request.body)["contents"][0]["parts"][0]["text"])
        assert context["last_control_feedback"] == "inspection: value_error"
    finally:
        adapter.close()


def test_invalid_provider_field_names_cannot_echo_credentials_in_feedback() -> None:
    invalid = json.dumps(
        {
            "action": "delegate",
            "rationale": "Inspect evidence",
            "plan": "Run check",
            KEY: "untrusted value",
        }
    )
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": invalid}]}}]}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure) as raised:
            adapter.decide(adapter.prepare(state()))
        assert KEY not in str(raised.value)
        assert "provider:extra_forbidden" in str(raised.value)


def test_supervisor_sees_remaining_allocation_and_delegation_guidance() -> None:
    current = replace(state(), decisions=7, spent_microusd=1234, reserved_microusd=100)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        body = json.loads(adapter.prepare(current).body)
    context = json.loads(body["contents"][0]["parts"][0]["text"])
    assert current.policy is not None
    assert context["decision_budget_remaining"] == current.policy.max_decisions - 7
    assert context["supervisor_allocation_remaining_microusd"] == 5_000_000 - 1334
    assert "parent cap can be lower" in context["budget_note"]
    assert "before delegating" in body["systemInstruction"]["parts"][0]["text"]
    assert context["allowed_actions"] == ["block", "delegate"]


@pytest.mark.parametrize("excluded", [False, True])
def test_original_evidence_reaches_request_and_stays_untrusted(
    tmp_path: Path, excluded: bool
) -> None:
    raw = "Earlier successful output\n" * 180 + (KEY if excluded else "ACTUAL_FAILURE_AT_END")
    record = ResearchRecord(
        record_id="baseline-original",
        operation_key="baseline",
        kind="baseline",
        revision="a" * 40,
        reference="fixed-checks",
        content_sha256=hashlib.sha256(raw.encode()).hexdigest(),
        summary="baseline",
        excerpt=raw[:2000],
        truncated=True,
    )
    store = ResearchStore(SQLiteGoalJournal(tmp_path / "goals.sqlite"))
    store.put("baseline", "intent", record, raw)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        adapter.evidence_store = store
        current = replace(state(), research=(record,))
        if excluded:
            with pytest.raises(SupervisorFailure, match="exclusion"):
                adapter.prepare(current)
        else:
            body = json.loads(adapter.prepare(current).body)
            context = json.loads(body["contents"][0]["parts"][0]["text"])
            evidence = context["investigation_state"]["working_evidence_untrusted"]
            assert evidence["records"][0]["content"] == raw
            assert evidence["records"][0]["complete_original"]
            assert "never policy" in evidence["trust"]
            assert context["allowed_actions"] == ["block", "delegate"]


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"max_request_bytes": 15_999}, "bounds"),
        ({"max_request_bytes": 400_001}, "bounds"),
        ({"max_request_bytes": 100_000, "working_evidence_bytes": 60_000}, "bounds"),
        ({"working_evidence_bytes": 3_999}, "bounds"),
    ],
)
def test_supervisor_request_limits_outside_bounds_are_rejected(
    limits: dict[str, int], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        GeminiSupervisor(KEY, **limits)  # type: ignore[arg-type]


def test_larger_operator_cap_retains_evidence_the_default_compacts() -> None:
    evidence = json.dumps({"last_verification": "x" * 60_000})
    big = replace(state(), observation=WorkerObservation(status="attention", evidence=evidence))
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        default = GeminiSupervisor(KEY, client=client)
        with pytest.raises(SupervisorFailure, match="exceeds admission"):
            default.prepare(big)
        larger = GeminiSupervisor(KEY, client=client, max_request_bytes=200_000)
        prepared = larger.prepare(big)
    assert "x" * 60_000 in prepared.body
    assert 60_000 < len(prepared.body.encode()) <= 200_000


def test_counted_tokens_tighten_but_never_loosen_the_reservation() -> None:
    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"totalTokens": 1000})

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        byte_bound = GeminiSupervisor(KEY, client=client).prepare(state())
        counted = GeminiSupervisor(KEY, client=client, count_tokens=True).prepare(state())
    assert calls == [f"/v1beta/models/{gemini_module.MODEL}:countTokens"]
    byte_trace = json.loads(byte_bound.trace)
    counted_trace = json.loads(counted.trace)
    assert counted_trace["measurement_basis"] == "count_tokens_plus_25_percent_plus_2048"
    assert counted_trace["input_bound"] <= byte_trace["input_bound"]
    assert counted.maximum_microusd <= byte_bound.maximum_microusd
    assert KEY not in counted.trace


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(500),
        httpx.Response(200, json={"totalTokens": "1000"}),
        httpx.Response(200, json={"totalTokens": 10**9}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_failed_or_implausible_token_count_falls_back_to_byte_bound(
    reply: httpx.Response,
) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: reply)) as client:
        prepared = GeminiSupervisor(KEY, client=client, count_tokens=True).prepare(state())
    trace = json.loads(prepared.trace)
    assert trace["measurement_basis"] == "utf8_byte_upper_bound_plus_4096"
    assert trace["input_bound"] == len(prepared.body.encode()) + 4096


def _assignment_reply(assignment: dict[str, object]) -> dict[str, object]:
    payload = response()
    part = payload["candidates"][0]["content"]["parts"][0]  # type: ignore[index]
    part["text"] = json.dumps(  # type: ignore[index]
        {"action": "delegate", "rationale": "r", "plan": "p", "assignment": assignment}
    )
    return payload


@pytest.mark.parametrize(
    ("assignment", "reason"),
    [
        (
            {
                "objective": "SECRET-OBJECTIVE fix it",
                "files": ["a.py", "a.py"],
                "evidence_ids": ["e1"],
            },
            "files must not repeat a path",
        ),
        (
            {
                "objective": "SECRET-OBJECTIVE fix it",
                "files": ["../etc/passwd"],
                "evidence_ids": ["e1"],
            },
            "files must be safe relative paths",
        ),
        (
            {
                "objective": "SECRET-OBJECTIVE fix it",
                "files": ["a.py"],
                "evidence_ids": ["e1", "e1"],
            },
            "evidence_ids must not repeat an id",
        ),
    ],
)
def test_invalid_assignment_names_the_rule_without_echoing_provider_values(
    assignment: dict[str, object], reason: str
) -> None:
    reply = _assignment_reply(assignment)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
    ) as c:
        adapter = GeminiSupervisor(KEY, client=c)
        with pytest.raises(SupervisorFailure) as failure:
            adapter.decide(adapter.prepare(state()))
    message = str(failure.value)
    assert f"assignment:value_error (Assignment invalid: {reason})" in message
    assert "SECRET-OBJECTIVE" not in message
    assert "passwd" not in message


def test_rejected_response_is_recorded_for_the_operator_only(tmp_path: Path) -> None:
    bad = response()
    bad["candidates"][0]["content"]["parts"][0]["text"] = "not json"  # type: ignore[index]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad))) as c:
        adapter = GeminiSupervisor(KEY, client=c, diagnostics=tmp_path / "diag")
        with pytest.raises(SupervisorFailure) as failure:
            adapter.decide(adapter.prepare(state()))
    records = list((tmp_path / "diag").glob("rejected-*.json"))
    assert len(records) == 1
    saved = json.loads(records[0].read_text())
    assert saved["reason"] == str(failure.value)
    assert "not json" in saved["response"]
    assert "not json" not in str(failure.value)


def test_response_containing_the_credential_is_never_recorded(tmp_path: Path) -> None:
    bad = response()
    bad["candidates"][0]["content"]["parts"][0]["text"] = KEY  # type: ignore[index]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad))) as c:
        adapter = GeminiSupervisor(KEY, client=c, diagnostics=tmp_path / "diag")
        with pytest.raises(SupervisorFailure):
            adapter.decide(adapter.prepare(state()))
    assert not (tmp_path / "diag").exists()


def _decide(decision: Mapping[str, object]) -> object:
    payload = response()
    candidate = payload["candidates"][0]  # type: ignore[index]
    candidate["content"]["parts"][0]["text"] = json.dumps(decision)  # type: ignore[index]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        adapter = GeminiSupervisor(KEY, client=client)
        return adapter.decide(adapter.prepare(state()))


RECOVERY = {
    "changed_hypothesis": "Restart must reset food to (10,10); keep #start-btn enabled.",
    "evidence_ids": ["e_worker"],
    "failure": "no_progress",
}


def test_retry_with_a_new_assignment_is_refused_with_the_accepted_fields_named() -> None:
    decision = {
        "action": "retry",
        "rationale": "Measured acceptance failures",
        "plan": "Retry with a fix",
        "recovery": RECOVERY,
        "assignment": {
            "objective": "Fix the measured acceptance failures in app/",
            "files": ["app/game.js"],
            "evidence_ids": ["e_worker"],
        },
    }
    with pytest.raises(SupervisorFailure) as failure:
        _decide(decision)
    assert str(failure.value) == (
        "Gemini returned an invalid decision envelope: Supervisor action arguments do not "
        "match its capability: retry accepts recovery"
    )
    assert "food" not in str(failure.value)  # Provider text is never echoed.
    retry = {k: v for k, v in decision.items() if k != "assignment"}
    result = _decide(retry)
    assert result.decision.action == GoalAction.RETRY  # type: ignore[attr-defined]


def test_recovery_schema_states_the_domain_limits() -> None:
    schema = DecisionPayload.model_json_schema()["properties"]["recovery"]["anyOf"][0]
    hypothesis = schema["properties"]["changed_hypothesis"]
    assert (hypothesis["minLength"], hypothesis["maxLength"]) == (20, 1000)
    assert "no_progress" in schema["properties"]["failure"]["enum"]
    assert "$ref" not in json.dumps(schema)
    with pytest.raises(SupervisorFailure, match="Recovery needs a concrete"):
        _decide(
            {
                "action": "retry",
                "rationale": "r",
                "plan": "p",
                "recovery": {**RECOVERY, "changed_hypothesis": "x" * 1001},
            }
        )
