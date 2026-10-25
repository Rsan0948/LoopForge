"""Token budgets count work: a re-sent conversation prefix is not charged again. No network."""

import json
from dataclasses import replace

import httpx

from loopforge.adapters.ollama_model import OllamaModel
from loopforge.domain.context import ModelContext
from loopforge.domain.policy import ControlPolicy
from loopforge.domain.state import RunState
from loopforge.domain.types import BudgetLimit, RunId
from loopforge.ports.model import ModelToolSpec
from tests.unit.test_ollama_model import (
    TEMPLATE,
    TOOLS,
    _chat_payload,  # pyright: ignore[reportPrivateUsage]
    _context,  # pyright: ignore[reportPrivateUsage]
)


def _replies(*payloads: dict[str, object]) -> tuple[httpx.MockTransport, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=payloads[len(sent) - 1])

    return httpx.MockTransport(handle), sent


def _history_model(transport: httpx.MockTransport) -> OllamaModel:
    return OllamaModel(
        model="m", tools=TOOLS, template=TEMPLATE, transport=transport, conversation_mode="history"
    )


def test_a_request_that_extends_the_last_one_is_charged_only_for_the_new_part() -> None:
    transport, sent = _replies(
        _chat_payload(prompt_tokens=10_000), _chat_payload(prompt_tokens=10_400)
    )
    model = _history_model(transport)
    first = model.propose_action(_context())
    second = model.propose_action(_context())
    assert sent[1]["messages"][: len(sent[0]["messages"])] == sent[0]["messages"]  # type: ignore[index]
    assert first.usage.cached_input_tokens == 0
    assert second.usage.cached_input_tokens == 10_000


def test_a_server_reported_cache_count_wins_and_never_exceeds_the_prompt() -> None:
    reported = {**_chat_payload(prompt_tokens=500), "prompt_eval_cached_count": 9_000}
    transport, _ = _replies(_chat_payload(prompt_tokens=400), reported)
    model = _history_model(transport)
    model.propose_action(_context())
    assert model.propose_action(_context()).usage.cached_input_tokens == 500


def test_a_rewritten_prefix_is_charged_in_full() -> None:
    transport, sent = _replies(
        _chat_payload(prompt_tokens=10_000), _chat_payload(prompt_tokens=10_400)
    )
    model = _history_model(transport)
    model.propose_action(_context())
    model.drop_conversation(_context().run_id)
    turn = model.propose_action(_context())
    assert turn.usage.cached_input_tokens == 0
    assert len(sent) == 2


def test_an_explicit_cache_miss_is_not_replaced_by_a_prefix_estimate() -> None:
    transport, _ = _replies(
        _chat_payload(prompt_tokens=10_000),
        {**_chat_payload(prompt_tokens=10_400), "prompt_eval_cached_count": 0},
    )
    model = _history_model(transport)
    model.propose_action(_context())
    assert model.propose_action(_context()).usage.cached_input_tokens == 0


def test_changing_tool_schemas_invalidates_the_estimated_cached_prefix() -> None:
    transport, _ = _replies(
        _chat_payload(prompt_tokens=10_000), _chat_payload(prompt_tokens=10_400)
    )
    version = 0

    def filter_spec(context: ModelContext, spec: ModelToolSpec) -> ModelToolSpec:
        del context
        return replace(spec, description=spec.description + str(version))

    model = OllamaModel(
        model="m",
        tools=TOOLS,
        template=TEMPLATE,
        transport=transport,
        conversation_mode="history",
        tool_spec_filter=filter_spec,
    )
    model.propose_action(_context())
    version = 1
    assert model.propose_action(_context()).usage.cached_input_tokens == 0


def test_the_token_budget_counts_work_tokens_not_repeated_input() -> None:
    policy = ControlPolicy(BudgetLimit(max_cost_usd=1, max_iterations=None, max_total_tokens=1000))
    state = replace(RunState(run_id=RunId("r")), input_tokens=5_000, output_tokens=200)
    assert policy.evaluate(state).reason_code == "STOP_TOKEN_BUDGET_EXHAUSTED"
    cached = replace(state, cached_input_tokens=4_500)
    assert cached.work_tokens == 700
    assert cached.total_tokens == 5_200  # reports keep the full count
    assert policy.evaluate(cached).reason_code == "CONTINUE_WITHIN_POLICY"
    over = replace(cached, cached_input_tokens=99_999)
    assert over.work_tokens == 200  # a cache count larger than input never goes negative
