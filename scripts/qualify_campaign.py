"""Opt-in serial local-model qualification. Never dispatches repository campaigns.

Runs twelve locked tasks three times through the actual review runtime and reporting
adapter, then the same tasks through a minimal schema-only reference. Results and
raw HTTP exchanges are retained locally. No unit test invokes this script.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

import httpx

from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.ollama_reporting import OllamaReporting
from loopforge.domain.actions import ActionProposal
from loopforge.domain.types import ActionId
from loopforge.entrypoints.campaign_manifest import release_manifest
from loopforge.entrypoints.campaign_models import CampaignConfig, ShortReferencedReport
from loopforge.entrypoints.campaign_qualification import grade_contract
from loopforge.entrypoints.campaign_quality import plan_workload
from loopforge.entrypoints.campaign_review import ReviewTools, build_review
from loopforge.ports.tools import ToolExecutionRequest

MODEL = "qwen3.6:35b-a3b"
VERSION = "locked-v10-directed-source-contract"

# Gold is fixed before inference. Last six cases are held out from prompt tuning.
CASES = [
    (
        "probe-count",
        True,
        {
            "current.md": "The PROBES list in impl.py contains exactly two entries.\n",
            "impl.py": "PROBES = ['database', 'auth', 'queue']\n",
        },
    ),
    (
        "timeout",
        True,
        {
            "current.md": "REQUEST_TIMEOUT_SECONDS in impl.py is configured as 30.\n",
            "impl.py": "REQUEST_TIMEOUT_SECONDS = 10\n",
        },
    ),
    (
        "retention",
        True,
        {
            "current.md": "LOG_RETENTION_DAYS in impl.py is configured as 7.\n",
            "impl.py": "LOG_RETENTION_DAYS = 30\n",
        },
    ),
    (
        "intentional-503",
        False,
        {
            "current.md": "Missing credentials intentionally return 503.\n",
            "impl.py": "def status(credentials):\n    return 200 if credentials else 503\n",
        },
    ),
    (
        "historical-doc",
        False,
        {
            "historical.md": ("Historical snapshot, superseded: REQUEST_TIMEOUT_SECONDS was 30.\n"),
            "current.md": "Current contract: REQUEST_TIMEOUT_SECONDS in impl.py is 10.\n",
            "impl.py": "REQUEST_TIMEOUT_SECONDS = 10\n",
        },
    ),
    (
        "planned-doc",
        False,
        {
            "plan.md": "Future proposal, not implemented: set LOG_RETENTION_DAYS to 90.\n",
            "current.md": "Current contract: LOG_RETENTION_DAYS in impl.py is 30.\n",
            "impl.py": "LOG_RETENTION_DAYS = 30\n",
        },
    ),
    (
        "heldout-limit",
        True,
        {
            "current.md": "MAX_RETURNED_RECORDS in impl.py is configured as 100.\n",
            "impl.py": "MAX_RETURNED_RECORDS = 50\n",
        },
    ),
    (
        "heldout-role",
        False,
        {
            "current.md": "The rights page is active for authenticated users.\n",
            "impl.py": "ROUTES = {'/rights': {'active': True, 'requires_auth': True}}\n",
        },
    ),
    (
        "heldout-retries",
        True,
        {
            "current.md": "MAX_RETRIES_AFTER_FAILURE in impl.py is configured as 2.\n",
            "impl.py": "MAX_RETRIES_AFTER_FAILURE = 4\n",
        },
    ),
    (
        "heldout-correct",
        False,
        {
            "current.md": "JOB_EXPIRY_SECONDS in impl.py is configured as 60.\n",
            "impl.py": "JOB_EXPIRY_SECONDS = 60\n",
        },
    ),
]

CASES.extend(
    [
        (
            "heldout-long-drift",
            True,
            {
                "current.md": "Current contract.\n"
                + "Unrelated context.\n" * 90
                + "MAX_WAIT_SECONDS in impl.py is configured as 30.\n",
                "impl.py": "# implementation\n"
                + "# helper context\n" * 90
                + "MAX_WAIT_SECONDS = 10\n",
            },
        ),
        (
            "heldout-long-correct",
            False,
            {
                "current.md": "Current contract.\n"
                + "Unrelated context.\n" * 90
                + "MAX_WAIT_SECONDS in impl.py is configured as 10.\n",
                "impl.py": "# implementation\n"
                + "# helper context\n" * 90
                + "MAX_WAIT_SECONDS = 10\n",
            },
        ),
    ]
)


# Frozen after prompt development. Never use failures here for prompt tuning
# while continuing to describe the same cases as held out.
FRESH_CASES = [
    (
        "fresh-buffer-caller",
        True,
        {
            "current.md": "BUFFER_LIMIT in impl.py is configured as 128.\n",
            "impl.py": "BUFFER_LIMIT = 64\n",
            "caller.py": "from impl import BUFFER_LIMIT\ndef limit(): return BUFFER_LIMIT\n",
        },
    ),
    (
        "fresh-page-contract",
        True,
        {
            "current.md": "Current contract: MAX_ACTIVE_PAGES in impl.py is 11.\n",
            "impl.py": "MAX_ACTIVE_PAGES = 9\n",
        },
    ),
    (
        "fresh-retry-contract",
        True,
        {
            "current.md": "Current contract: RETRY_INTERVAL_MS in impl.py is 250.\n",
            "impl.py": "RETRY_INTERVAL_MS = 125\n",
        },
    ),
    (
        "fresh-historical-match",
        False,
        {
            "historical.md": "Historical snapshot, superseded: BATCH_SIZE was 200.\n",
            "current.md": "Current contract: BATCH_SIZE in impl.py is 40.\n",
            "impl.py": "BATCH_SIZE = 40\n",
        },
    ),
    (
        "fresh-plan-match",
        False,
        {
            "plan.md": "Future proposal, not implemented: set LEASE_SECONDS to 400.\n",
            "current.md": "Current contract: LEASE_SECONDS in impl.py is 120.\n",
            "impl.py": "LEASE_SECONDS = 120\n",
        },
    ),
    (
        "fresh-working-denial",
        False,
        {
            "current.md": "Missing tokens intentionally return 401.\n",
            "impl.py": "def status(token):\n    return 200 if token else 401\n",
        },
    ),
    (
        "fresh-late-drift",
        True,
        {
            "current.md": "Current contract.\n"
            + "Background text.\n" * 180
            + "MAX_BATCH_ITEMS in impl.py is configured as 500.\n",
            "impl.py": "# source\n" + "# helper notes\n" * 180 + "MAX_BATCH_ITEMS = 100\n",
        },
    ),
    (
        "fresh-late-match",
        False,
        {
            "current.md": "Current contract.\n"
            + "Background text.\n" * 180
            + "POOL_EXPIRY_SECONDS in impl.py is configured as 76.\n",
            "impl.py": "# source\n" + "# helper notes\n" * 180 + "POOL_EXPIRY_SECONDS = 76\n",
        },
    ),
]


def grade(report, expected, paths):
    return grade_contract(report, expected, paths) if isinstance(paths, dict) else False


class QualificationStopped(Exception):
    """A completed failing trial ended an explicitly fail-fast experiment."""


def main():  # noqa: PLR0912, PLR0915 - serial qualification and evidence retention
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-only", action="store_true")
    parser.add_argument("--harness-only", action="store_true")
    parser.add_argument("--repetitions", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--fresh-holdout", action="store_true")
    args = parser.parse_args()
    cases = FRESH_CASES if args.fresh_holdout else CASES
    args.output.mkdir(parents=True, exist_ok=False)
    with httpx.Client(timeout=15) as client:
        campaigns = client.get("http://localhost:8124/campaigns").raise_for_status().json()
        assert all(
            c["state"]["status"] == "parked" and c["slot"] is None and not c["worker_alive"]
            for c in campaigns["campaigns"]
        )
        tags = client.get("http://localhost:11434/api/tags").raise_for_status().json()
        model = next(m for m in tags["models"] if m["name"] == MODEL)
        version = client.get("http://localhost:11434/api/version").raise_for_status().json()
    identity = {
        "version": VERSION + ("-fresh-holdout" if args.fresh_holdout else "-development"),
        "loaded_manifest": release_manifest(),
        "cases_sha256": hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        "model": model,
        "provider": version,
        "settings": {
            "temperature": 0,
            "think": False,
            "num_ctx": 32768,
            "num_predict": 2048,
            "reporter_presence_penalty": 0.0,
        },
    }
    (args.output / "identity.json").write_text(json.dumps(identity, indent=2))
    lease = ModelLease()
    lease.acquire("qualification:" + str(args.output))
    real_send = httpx.Client.send
    exchanges = []

    def send(client, request, **kwargs):
        if request.url.port != 11434:
            return real_send(client, request, **kwargs)
        started = time.monotonic()
        response = real_send(client, request, **kwargs)
        exchanges.append(
            {
                "request": json.loads(request.content),
                "response": response.json(),
                "seconds": time.monotonic() - started,
            }
        )
        return response

    httpx.Client.send = send
    results = []
    try:
        modes = (
            [("reference", 1)]
            if args.reference_only
            else [("harness", args.repetitions)]
            if args.harness_only
            else [("harness", args.repetitions), ("reference", 1)]
        )
        selected = [case for case in cases if not args.case or case[0] in args.case]
        if not selected or set(args.case) - {case[0] for case in cases}:
            msg = "Unknown qualification case"
            raise ValueError(msg)
        for mode, repetitions in modes:
            for repeat in range(repetitions):
                for name, expected, files in selected:
                    exchanges.clear()
                    started = time.monotonic()
                    objective = (
                        "Check only whether CURRENT documentation contradicts "
                        "the supplied full file. "
                        "Historical snapshots and future proposals are not current contracts. "
                        "This is a bounded source claim, not a runtime execution claim. "
                        "Do not speculate about unseen overrides. Do not invent additional "
                        "defects. A contradiction should be one "
                        "suspected finding citing BOTH current documentation and implementation; "
                        "a matching contract has no actionable finding. Read ALL these files: "
                        + json.dumps(list(files))
                    )
                    report, status, events = None, "error", []
                    try:
                        if mode == "harness":
                            config = CampaignConfig(
                                repository="/qualification",
                                project="brook",
                                revision="a" * 40,
                                scope=["."],
                                max_iterations=12,
                                max_tokens=100_000,
                                max_seconds=300,
                                model_timeout_seconds=120,
                            )
                            planned = plan_workload(files, ".", "docs", 1)
                            objective += (
                                "\nThis fixture requires full-file coverage; "
                                "planner-selected intervals are navigation only."
                            )
                            objective += (
                                "\nMandatory coverage for this full-file fixture: "
                                + json.dumps(
                                    {p: [(1, len(t.splitlines()))] for p, t in files.items()}
                                )
                            )
                            store = InMemoryEventStore()
                            runtime, tools, live = build_review(
                                config, files=files, previous="", store=store, max_source_reads=8
                            )
                            tools.required_intervals = {
                                p: [(1, len(t.splitlines()))] for p, t in files.items()
                            }
                            # Production preselected workloads finalize mechanically after
                            # mandatory reads; free-form discovery is a separate contract.
                            tools.finalize_when_covered = True
                            try:
                                state = runtime.run(objective)
                                report, status = tools.report, state.status.value
                                codec = JsonEventCodec()
                                events = [codec.encode(e) for e in store.events_for(state.run_id)]
                            finally:
                                if live:
                                    live.close()
                        else:
                            tools = ReviewTools(files, "")
                            observed = []
                            for path, text in files.items():
                                read = tools.execute(
                                    ToolExecutionRequest(
                                        ActionProposal(
                                            ActionId("reference-read"), "read_file", {"path": path}
                                        ),
                                        1,
                                        5,
                                    )
                                )
                                assert read.ok
                                key = tools.register_excerpt(path, 1, text.rstrip("\n"))
                                observed.append(
                                    {
                                        "excerpt_id": key,
                                        "path": path,
                                        "start_line": 1,
                                        "text": text.rstrip("\n"),
                                    }
                                )
                            reporter = OllamaReporting(MODEL, timeout_seconds=120)
                            try:
                                response = reporter.generate(
                                    json.dumps(
                                        {
                                            "trust": "untrusted_content",
                                            "task_context": [],
                                            "earlier_evidence": "No earlier reports.",
                                            "observed": observed,
                                        }
                                    ),
                                    ShortReferencedReport.model_json_schema(),
                                    objective=objective,
                                )
                                report = tools.referenced_report(
                                    ShortReferencedReport.model_validate_json(
                                        response.content
                                    ).referenced()
                                )
                            finally:
                                reporter.close()
                            status = "succeeded"
                        correct = status == "succeeded" and grade(report, expected, files)
                        error = None
                    except Exception as exc:
                        correct, error = False, str(exc)[:2000]
                    record = {
                        "mode": mode,
                        "repeat": repeat,
                        "case": name,
                        "gold_drift": expected,
                        "held_out": args.fresh_holdout,
                        "correct": correct,
                        "status": status,
                        "error": error,
                        "seconds": time.monotonic() - started,
                        "report": report.model_dump() if report else None,
                        "planner_contract": planned.model_dump() if mode == "harness" else None,
                        "exchanges": list(exchanges),
                        "events": events,
                    }
                    results.append(record)
                    key = f"{mode}-{repeat}-{name}"
                    (args.output / f"{key}.json").write_text(json.dumps(record, indent=2))
                    print(
                        json.dumps(
                            {
                                k: record[k]
                                for k in ["mode", "repeat", "case", "correct", "status", "seconds"]
                            }
                        ),
                        flush=True,
                    )
                    if args.fail_fast and not correct:
                        raise QualificationStopped  # noqa: TRY301 - end completed fail-fast trial
    except QualificationStopped:
        pass
    finally:
        httpx.Client.send = real_send
        lease.release()
    harness = [r for r in results if r["mode"] == "harness"]
    summary = {
        "identity": identity,
        "trials": len(harness),
        "correct": sum(r["correct"] for r in harness),
        "gate_passed": len(harness) >= len(cases) * 3
        and sum(r["correct"] for r in harness) / len(harness) >= 0.9
        and all(r["correct"] for r in harness if not r["gold_drift"]),
        "limitations": "Small synthetic suite; citations host-validated. No repair or "
        "sandbox/n8n qualification. Positive grader requires the actual contradiction, "
        "direction and both exact source quotes; not proof of general repair quality.",
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "identity"}), flush=True)


if __name__ == "__main__":
    main()
