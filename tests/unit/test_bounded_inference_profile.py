"""Opt-in context envelopes preserve legacy limits and fail closed above capacity."""

import json
from datetime import UTC, datetime
from typing import cast

import httpx
import pytest

from loopforge.adapters.ollama_profiles import profile_for
from loopforge.adapters.ollama_reporting import OllamaReporting, report_payload
from loopforge.domain.context import ModelContext
from loopforge.domain.types import RunId
from loopforge.entrypoints.campaign_review import ReviewTools, build_report_packet
from loopforge.ports.model import ModelTurnError


@pytest.mark.parametrize("role", ["coding", "review", "reporter"])
def test_bounded_profile_is_explicit_and_bounded(role: str) -> None:
    selected = profile_for(role, bounded_v2=True)  # type: ignore[arg-type]
    assert selected.context_tokens == 65536
    assert not selected.thinking
    assert not selected.preserve_thinking
    assert selected.output_tokens + 1024 + 56000 <= selected.context_tokens


@pytest.mark.parametrize(("size", "passes"), [(30000, True), (60000, False)])
def test_report_envelope_bounds_real_serialized_request(size: int, passes: bool) -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        assert len(request.content) <= 56000
        assert json.loads(request.content)["options"]["num_ctx"] == 65536
        return httpx.Response(
            200,
            json={
                "message": {"content": "{}"},
                "done": True,
                "prompt_eval_count": 100,
                "eval_count": 2,
            },
        )

    reporter = OllamaReporting("m", bounded_v2=True, transport=httpx.MockTransport(respond))
    try:
        if passes:
            assert reporter.generate("x" * size, {}).content == "{}"
        else:
            with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
                reporter.generate("x" * size, {})
        assert bool(sent) is passes
    finally:
        reporter.close()
    assert cast("dict[str, object]", report_payload("m", "", {}, "")["options"])["num_ctx"] == 32768


@pytest.mark.parametrize(("size", "passes"), [(30000, True), (60000, False)])
def test_larger_report_never_discards_mandatory_source(size: int, passes: bool) -> None:
    tools = ReviewTools({"a.py": "x" * size}, "")
    tools.bounded_reporting = True
    tools.register_excerpt("a.py", 1, "x" * size)
    tools.required_intervals = {"a.py": [(1, 1)]}
    context = ModelContext(run_id=RunId("bounded"), items=(), assembled_at=datetime.now(UTC))
    before = dict(tools.excerpts)
    if passes:
        packet = json.loads(build_report_packet(tools, context, "Inspect exact source"))
        assert packet["observed"][0]["numbered_text"] == "1: " + "x" * size
    else:
        with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
            build_report_packet(tools, context, "Inspect exact source")
    assert tools.excerpts == before


@pytest.mark.parametrize(("size", "passes"), [(75000, True), (90000, False)])
def test_v3_report_capacity_is_separate_and_remains_bounded(size: int, passes: bool) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["options"]["num_ctx"] == 98304
        assert len(request.content) <= 88000
        assert len(request.content) + 3072 + 1024 <= 98304
        return httpx.Response(
            200,
            json={
                "message": {"content": "{}"},
                "done": True,
                "prompt_eval_count": 100,
                "eval_count": 2,
            },
        )

    reporter = OllamaReporting("m", report_v3=True, transport=httpx.MockTransport(respond))
    try:
        if passes:
            reporter.generate("x" * size, {})
        else:
            with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
                reporter.generate("x" * size, {})
        assert bool(requests) is passes
    finally:
        reporter.close()
    assert profile_for("reporter", bounded_v2=True).context_tokens == 65536
    assert profile_for("coding", bounded_v2=True).context_tokens == 65536


@pytest.mark.parametrize("large", [False, True])
def test_v3_preserves_evidence_that_v2_cannot_admit(large: bool) -> None:
    tools = ReviewTools({"a.py": "x" * 60000}, "")
    tools.bounded_reporting = True
    tools.reporting_v3 = large
    tools.register_excerpt("a.py", 1, "x" * 60000)
    tools.required_intervals = {"a.py": [(1, 1)]}
    context = ModelContext(run_id=RunId("larger"), items=(), assembled_at=datetime.now(UTC))
    if large:
        packet = json.loads(build_report_packet(tools, context, "Inspect exact source"))
        assert packet["observed"][0]["numbered_text"] == "1: " + "x" * 60000
    else:
        with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
            build_report_packet(tools, context, "Inspect exact source")
    assert tools.files["a.py"] == "x" * 60000


@pytest.mark.parametrize(("size", "passes"), [(140000, True), (180000, False)])
def test_app_profile_reserves_output_and_rejects_oversized_report(size: int, passes: bool) -> None:
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        payload = json.loads(request.content)
        assert payload["options"]["num_ctx"] == 196608
        assert payload["options"]["num_predict"] == 2048
        assert len(request.content) + 2048 + 1024 <= 196608
        return httpx.Response(
            200,
            json={
                "message": {"content": "{}"},
                "done": True,
                "prompt_eval_count": 100,
                "eval_count": 2,
            },
        )

    reporter = OllamaReporting("m", app_workload=True, transport=httpx.MockTransport(respond))
    try:
        if passes:
            reporter.generate("x" * size, {})
        else:
            with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
                reporter.generate("x" * size, {})
        assert bool(sent) is passes
    finally:
        reporter.close()
    coding = profile_for("coding", app_workload=True)
    assert coding.context_tokens == 98304
    assert coding.output_tokens == 4096
    assert not coding.thinking
    assert not coding.preserve_thinking
    assert profile_for("reporter", report_v3=True).context_tokens == 98304


@pytest.mark.parametrize("app", [False, True])
def test_app_envelope_preserves_multi_file_evidence_and_legacy_bounds(app: bool) -> None:
    tools = ReviewTools({f"file{i}.py": "x" * 14000 for i in range(7)}, "")
    tools.app_workload = app
    tools.bounded_reporting = True
    tools.reporting_v3 = True
    tools.required_intervals = {name: [(1, 1)] for name in tools.files}
    for name, content in tools.files.items():
        tools.register_excerpt(name, 1, content)
    before = dict(tools.excerpts)
    context = ModelContext(run_id=RunId("app-packet"), items=(), assembled_at=datetime.now(UTC))
    if app:
        data = json.loads(build_report_packet(tools, context, "Review every required byte"))
        assert len(data["observed"]) == 7
        assert data["omitted_observed_excerpts"] == 0
        assert all(source["numbered_text"] == "1: " + "x" * 14000 for source in data["observed"])
    else:
        with pytest.raises(ModelTurnError, match="immutable reporting envelope"):
            build_report_packet(tools, context, "Review every required byte")
    assert tools.excerpts == before
