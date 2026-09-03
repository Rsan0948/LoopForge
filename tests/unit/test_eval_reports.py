"""EvalReportStore: operator-owned JSON persistence for eval reports (PACS-016, M6).

Pins the store contract without any sandbox: exact round-trip of every
``BenchmarkReport``/``ConfigReport`` field, atomic writes (stale-tmp sweep at
open, no residue after save), and fail-closed reads — corrupted JSON, schema
version drift, key drift, hand-tampered rates, and file/content id mismatches
all raise ``EvalReportStoreError`` instead of being repaired or skipped. The
domain constructors are the validation authority: a hand-edited rate that
disagrees with its counts can never load.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from loopforge.adapters.scripted import FixedClock
from loopforge.domain.benchmarks import BenchmarkReport, ConfigReport
from loopforge.entrypoints.eval import (
    REPORT_SCHEMA_VERSION,
    EvalReportStore,
    EvalReportStoreError,
)

EARLIER = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 3, 11, 30, tzinfo=UTC)
LOCK_HASH = "0123456789abcdef" * 4


def _entry(  # noqa: PLR0913 - report fixture helper keeps every field explicit
    config_id: str,
    task_id: str,
    *,
    trials: int = 2,
    successes: int = 1,
    false_successes: int = 0,
    mean_cost_usd: float = 0.02,
    mean_latency_seconds: float = 1.5,
    mean_total_tokens: float = 240.0,
    mean_human_interventions: float = 0.0,
) -> ConfigReport:
    return ConfigReport(
        config_id=config_id,
        task_id=task_id,
        trials=trials,
        successes=successes,
        false_successes=false_successes,
        success_rate=successes / trials,
        false_success_rate=false_successes / trials,
        mean_cost_usd=mean_cost_usd,
        mean_latency_seconds=mean_latency_seconds,
        mean_total_tokens=mean_total_tokens,
        mean_human_interventions=mean_human_interventions,
    )


def _report(report_id: str = "eval-report-1") -> BenchmarkReport:
    entries = (
        _entry("cfg-a", "bench-transient-api", successes=2),
        _entry(
            "cfg-a",
            "bench-provider-outage",
            successes=0,
            mean_cost_usd=0.0,
            mean_total_tokens=0.0,
            mean_latency_seconds=0.25,
        ),
        _entry(
            "cfg-b",
            "bench-transient-api",
            successes=1,
            mean_cost_usd=0.01,
            mean_total_tokens=120.0,
        ),
        _entry(
            "cfg-b",
            "bench-provider-outage",
            successes=0,
            mean_cost_usd=0.0,
            mean_total_tokens=0.0,
            mean_latency_seconds=0.25,
        ),
    )
    return BenchmarkReport(
        report_id=report_id,
        suite_version="1.0.0",
        lock_hash=LOCK_HASH,
        config_reports=entries,
        pareto_config_ids=("cfg-a", "cfg-b"),
    )


def _rewrite(
    directory: Path,
    report_id: str,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    path = directory / f"{report_id}.json"
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(payload, dict)
    mutate(payload)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    report = _report()

    store.save(report)
    loaded = store.load(report.report_id)

    # Frozen-dataclass equality spans every field of every entry.
    assert loaded == report
    entry = loaded.config_reports[0]
    assert entry.trials == 2
    assert entry.successes == 2
    assert entry.success_rate == 1.0
    assert entry.mean_cost_usd == 0.02
    assert entry.mean_latency_seconds == 1.5
    assert entry.mean_total_tokens == 240.0
    assert entry.mean_human_interventions == 0.0


def test_save_writes_one_json_file_atomically(tmp_path: Path) -> None:
    directory = tmp_path / "reports"
    store = EvalReportStore(directory, clock=FixedClock(EARLIER))

    path = store.save(_report())

    assert path == directory / "eval-report-1.json"
    assert path.is_file()
    assert list(directory.glob("*.tmp")) == []
    payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    assert isinstance(payload, dict)
    assert set(payload) == {"schema_version", "created_at", "report"}
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION
    assert payload["created_at"] == EARLIER.isoformat()


def test_opening_store_sweeps_stale_tmp_files(tmp_path: Path) -> None:
    stale = tmp_path / "eval-interrupted.1234.tmp"
    stale.write_text("{}", encoding="utf-8")

    EvalReportStore(tmp_path)

    assert not stale.exists()


def test_load_unknown_report_id_fails_closed(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))

    with pytest.raises(EvalReportStoreError, match="unknown eval report 'eval-missing'"):
        store.load("eval-missing")


@pytest.mark.parametrize(
    "bad_id",
    ["../evil", "bad/id", "", ".hidden", "white space", "x" * 129],
    ids=["traversal", "slash", "empty", "leading-dot", "space", "too-long"],
)
def test_unsafe_report_ids_are_denied_on_load(tmp_path: Path, bad_id: str) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))

    with pytest.raises(EvalReportStoreError, match="not safe for the report store"):
        store.load(bad_id)


def test_unsafe_report_id_is_denied_on_save(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    report = _report()
    # The domain id validator already rejects control characters; the store's
    # filename safety check is defense in depth against every in-memory caller,
    # so pin it directly by mutating the frozen value.
    object.__setattr__(report, "report_id", "bad/id")

    with pytest.raises(EvalReportStoreError, match="not safe for the report store"):
        store.save(report)
    assert list(tmp_path.glob("*.json")) == []


def test_corrupt_json_fails_closed_on_load_and_list(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    store.save(_report())
    path = tmp_path / "eval-report-1.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(EvalReportStoreError, match="not valid JSON"):
        store.load("eval-report-1")
    with pytest.raises(EvalReportStoreError, match="not valid JSON"):
        store.list()


def test_wrong_schema_version_fails_closed(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    store.save(_report())

    def mutate(payload: dict[str, object]) -> None:
        payload["schema_version"] = 999

    _rewrite(tmp_path, "eval-report-1", mutate)

    with pytest.raises(EvalReportStoreError, match="schema version 999"):
        store.load("eval-report-1")


def test_drifted_report_keys_fail_closed(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    store.save(_report())

    def mutate(payload: dict[str, object]) -> None:
        report = payload["report"]
        assert isinstance(report, dict)
        del report["lock_hash"]

    _rewrite(tmp_path, "eval-report-1", mutate)

    with pytest.raises(EvalReportStoreError, match="keys drifted"):
        store.load("eval-report-1")


def test_tampered_rate_fails_domain_revalidation(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    store.save(_report())

    def mutate(payload: dict[str, object]) -> None:
        report = payload["report"]
        assert isinstance(report, dict)
        entries = cast("list[object]", report["config_reports"])
        entry = cast("dict[str, object]", entries[0])
        entry["success_rate"] = 0.9  # hand-tampered: disagrees with successes / trials

    _rewrite(tmp_path, "eval-report-1", mutate)

    with pytest.raises(EvalReportStoreError, match="fails domain revalidation"):
        store.load("eval-report-1")


def test_report_id_mismatch_between_file_and_content_fails_closed(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    store.save(_report())

    def mutate(payload: dict[str, object]) -> None:
        report = payload["report"]
        assert isinstance(report, dict)
        report["report_id"] = "eval-other"

    _rewrite(tmp_path, "eval-report-1", mutate)

    with pytest.raises(EvalReportStoreError, match="carries report_id 'eval-other'"):
        store.load("eval-report-1")


def test_non_object_envelope_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "eval-report-1.json"
    path.write_text('["not", "an", "object"]', encoding="utf-8")
    store = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))

    with pytest.raises(EvalReportStoreError, match="must be a JSON object"):
        store.load("eval-report-1")


def test_list_is_empty_when_directory_is_missing(tmp_path: Path) -> None:
    store = EvalReportStore(tmp_path / "nope", clock=FixedClock(EARLIER))

    assert store.list() == ()


def test_list_summarizes_reports_in_created_at_order(tmp_path: Path) -> None:
    # Saved in reverse chronological order; summaries must sort by created_at.
    first = EvalReportStore(tmp_path, clock=FixedClock(LATER))
    first.save(_report("eval-later"))
    second = EvalReportStore(tmp_path, clock=FixedClock(EARLIER))
    second.save(_report("eval-earlier"))

    summaries = EvalReportStore(tmp_path).list()

    assert [summary.report_id for summary in summaries] == ["eval-earlier", "eval-later"]
    earlier = summaries[0]
    assert earlier.suite_version == "1.0.0"
    assert earlier.lock_hash == LOCK_HASH
    assert earlier.config_ids == ("cfg-a", "cfg-b")
    assert earlier.task_ids == ("bench-provider-outage", "bench-transient-api")
    assert earlier.created_at == EARLIER.isoformat()
    assert summaries[1].created_at == LATER.isoformat()
