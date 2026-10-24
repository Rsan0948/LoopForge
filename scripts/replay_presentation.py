"""Offline: replay a recorded trial's contexts through the adapter, old vs new presentation.

Feeds each recorded ContextAssembled snapshot to OllamaModel with the recorded model
responses (no inference) and reports the request size per call under:
  legacy  - plain observations, no usage budget (the trial's own configuration)
  current - the current A3B profile: delimited observations, failure fingerprints,
            checkpoint compaction under its working budget
The coding wrapper's extra items (contract, bookkeeping) and tool-side changes
(12K reads, edit echo, warnings filtering at the source) are not reproduced, so this
measures presentation and compaction only. Sizes are estimated tokens calibrated to
the trial's recorded provider counts.

Usage: .venv/bin/python scripts/replay_presentation.py TRIAL_DIR
"""

import json
import sqlite3
import sys
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.ollama_model import OllamaModel
from loopforge.adapters.ollama_profiles import profile_for
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.domain.context import ContextItem, ModelContext
from loopforge.domain.events import ContextAssembled
from loopforge.domain.prompts import default_controller_template
from loopforge.ports.model import ModelToolSpec


def replay(trial: Path, *, current: bool) -> list[tuple[int, int]]:
    exchanges = [json.loads(p.read_text()) for p in sorted(trial.glob("exchange-*.json"))]
    exchanges = [e for e in exchanges if "response" in e]
    store = SQLiteEventStore(trial / "events.sqlite", codec=JsonEventCodec())
    with sqlite3.connect(trial / "events.sqlite") as db:
        run = db.execute("select run_id from events limit 1").fetchone()[0]
    events = store.events_for(run)
    contexts = [e for e in events if isinstance(e, ContextAssembled)]
    template = default_controller_template()
    tools = tuple(
        ModelToolSpec(
            name=t["function"]["name"],
            description=t["function"]["description"],
            parameters=t["function"]["parameters"],
        )
        for t in exchanges[0]["native_request"]["tools"]
    )
    sizes: list[tuple[int, int]] = []
    calls = iter(exchanges)

    def respond(request: httpx.Request) -> httpx.Response:
        recorded = next(calls)
        sizes.append((len(request.content), recorded["response"]["prompt_eval_count"]))
        return httpx.Response(200, json=recorded["response"])

    profile = profile_for("coding", native=True, model="qwen3.6:35b-a3b")
    model = OllamaModel(
        model="qwen3.6:35b-a3b",
        tools=tools,
        template=template,
        thinking=True,
        preserve_thinking=True,
        history_replay="exact",
        context_window_tokens=131072,
        max_output_tokens=profile.output_tokens,
        byte_ratio_admission=True,
        observation_format="delimited" if current else "plain",
        working_budget_tokens=profile.working_budget_tokens if current else None,
        transport=httpx.MockTransport(respond),
    )
    for event in contexts[: len(exchanges)]:
        items = tuple(
            ContextItem(
                item_id=s.item_id,
                content=s.content,
                trust=s.trust,
                source=s.source,
                sensitivity=s.sensitivity,
                created_at=s.created_at,
            )
            for s in event.context_items
        )
        context = ModelContext(
            run_id=event.run_id,
            items=items,
            assembled_at=event.occurred_at,
            prompt_template=template.reference(),
        )
        try:
            model.propose_action(context)
        except StopIteration:
            break
        except Exception as exc:
            print("replay stopped:", type(exc).__name__, str(exc)[:200])
            break
    return sizes


def main() -> None:
    trial = Path(sys.argv[1])
    legacy = replay(trial, current=False)
    current = replay(trial, current=True)
    ratio = sum(t for _, t in legacy) / sum(b for b, _ in legacy)
    print(f"tokens/byte calibrated on legacy replay vs recorded counts: {ratio:.3f}")
    print(f"{'call':>4} {'recorded':>9} {'legacy est':>10} {'current est':>11}")
    for i, ((lb, recorded), (cb, _)) in enumerate(zip(legacy, current, strict=False), start=1):
        print(f"{i:4} {recorded:9} {int(lb * ratio):10} {int(cb * ratio):11}")


if __name__ == "__main__":
    main()
