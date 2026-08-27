# PACS-008 — Observability foundation

Status: SUCCESS

## Objective

Make every important runtime decision explainable operationally without treating logs as
authoritative state.

## Why now

PACS-001–007 built a deterministic, event-sourced runtime with budgeted, provenance-aware
context, but the only execution record was the authoritative event stream itself: no spans, no
metrics, no structured logs, and no correlation identifiers beyond what individual events carry.
Every later cycle that operates or evaluates the runtime (PACS-011 live models, PACS-012 routing
telemetry, PACS-015 provenance/trajectory debugging, PACS-016 evaluation laboratory) needs an
observability projection that is OpenTelemetry-compatible, redacted, and fail-safe first.
PACS-006/007 deliberately left the seams this cycle consumes: per-item `DataSensitivity`
(SECRET already barred from persistence), the `BudgetedContextBuilder.last_accounting` ledger,
`ContextAssembled` template metadata, and the `UsageDelta` token/cost/cache fields.

## Dependencies

- PACS-001–005 (SUCCESS): authoritative event history, reliability control plane, sandbox contract.
- PACS-006 (SUCCESS): `DataSensitivity` redaction boundary; SECRET persistence rejection.
- PACS-007 (SUCCESS): accounting ledger seam, prompt template metadata, budgeted builder.

## In scope

- `domain/telemetry.py`: OpenTelemetry-compatible telemetry vocabulary (span kinds/status,
  log severities, metric kinds, closed `SpanName`/`MetricName` vocabularies, `CorrelationIds`,
  `Span`/`LogRecord`/`MetricSample` records) plus the code-owned redaction model
  (`SensitiveText`, `redact_text`/`redact_attributes`/`redact_record`).
- `ports/telemetry.py`: `TelemetryPort` protocol (non-authoritative sink), `TelemetryEmissionError`,
  and the `FailSafeTelemetry` emission boundary (redaction + exception containment).
- `ports/context.py`: optional `ContextAccountingSource` runtime-checkable seam exposing
  `last_accounting` without coupling the builder contract to observability.
- `application/telemetry.py`: `RuntimeTelemetry` facade (deterministic per-run trace state,
  span context manager with stack-based parentage, run/cycle correlation, context-accounting
  metrics) and the event projector mapping every domain event to structured logs and metrics.
- Runtime wiring: spans for policy decision, context build, model boundary, tool execution,
  verifier, persistence, retry, and the root run span; metric/log projection after every
  durable append; optional `telemetry` constructor field (defaults to a no-op sink).
- Correlation identifiers: run/cycle/action/tool/attempt/verification on every record;
  `worker_id` reserved in the vocabulary for PACS-013; verification ids derived from the
  authoritative event sequence (`{run_id}:verification:{sequence}`).
- Token/cost/cache accounting: per-turn usage on model-boundary spans and
  `loopforge.tokens.*` / `loopforge.cost.usd` metrics from `BudgetDebited`, ready for live models.
- Redaction before emission: SENSITIVE/SECRET tagged text is replaced with `[redacted]` at the
  fail-safe boundary before any adapter sees a record; unknown-sensitivity tool outcomes fail
  closed to redaction.
- `adapters/telemetry.py`: `NoOpTelemetry`, `InMemoryTelemetry` (deterministic recorder),
  OTLP/JSON-shaped converters (`to_otlp_span`/`to_otlp_metric`/`to_otlp_log`) proving
  OpenTelemetry compatibility through the data model, and `TelemetrySandbox` emitting
  sandbox-execution spans.
- Metrics for runs (started/completed/duration/outcome), cycles, retries, circuits, stalls,
  budget stops, tool failures, verification failures, context size/compaction, approvals, and
  token/cost/cache usage.
- CLI demo: prints a causally correlated trace/log/metric narrative with visible redaction.

## Out of scope

- Live/network exporters and collectors (rule 9; OTel compatibility is proven through the data
  model and in-memory recording only).
- Live models (PACS-011) — usage fields are present but debited by scripted models.
- Hardened container sandbox (PACS-009) — `TelemetrySandbox` wraps the existing local adapter.
- Adaptive policy (PACS-017) — telemetry never feeds back into runtime decisions.

## Plan

1. Domain vocabulary + redaction model (`domain/telemetry.py`), `VerificationId` in `domain/types.py`.
2. Port + fail-safe boundary (`ports/telemetry.py`); accounting seam (`ports/context.py`).
3. Emission facade + event projector (`application/telemetry.py`).
4. Runtime instrumentation at the drive-loop boundaries and `_persist` choke point.
5. Adapters: no-op, in-memory recorder, OTLP converters, sandbox span wrapper.
6. Demo narrative in the CLI; deterministic tests for every acceptance-gate criterion.
7. Reconcile roadmap/build-status/handoff documentation.

## Act

Implemented as planned. Notable decisions and discoveries:

- **One emission tap, one redaction point.** Every domain event still flows through
  `Runtime._persist`; telemetry is projected only *after* the authoritative append succeeds,
  and every record crosses `FailSafeTelemetry`, which redacts `SensitiveText` attributes
  (SENSITIVE/SECRET → `[redacted]`) before delegating and swallows any adapter exception into
  a `dropped_records` counter. A telemetry adapter that throws on every call cannot corrupt run
  state: the durable event stream is byte-identical (modulo minted ids) to a no-telemetry run
  (`test_throwing_telemetry_adapter_cannot_corrupt_run_state`).
- **Deterministic, causally nested traces.** Trace id = run id; the root run span
  (`{run_id}:span:0`) is emitted once at terminal stop (status from the stop reason: OK for
  success, UNSET for cancellation, ERROR otherwise); per-cycle spans parent to the root and
  operation spans parent to their cycle via a call-stack parent stack. Span sequence numbers are
  per-run monotonic, so equivalent runs produce equivalent narratives after run-id
  normalization (`test_equivalent_runs_produce_equivalent_telemetry`).
- **Verification correlation ties telemetry to the authoritative history.** The verification
  correlation id is derived from the sequence the verification event will be appended with, so
  the verify span, the projected log, and the durable event all name the same point in history
  (`test_verification_correlation_id_links_span_log_and_authoritative_sequence`).
- **Redaction is code-owned and fails closed.** Tool outcome text is tagged with the
  authorized tool's `DataSensitivity` (learned from `ActionAuthorized`); when no authorization
  is on record, outcomes are treated as SENSITIVE and redacted. The demo marks the `fix` tool
  SENSITIVE: its observation appears as `[redacted]` in telemetry while remaining intact in the
  event store, which stays authoritative (`test_sensitive_tool_observation_is_redacted_in_telemetry_but_authoritative_in_store`).
  SECRET content never reaches telemetry at all (PACS-006 persistence rejection is unchanged).
- **OTel compatibility without an SDK or network.** The domain vocabulary mirrors OTel span
  kinds/status codes and metric instruments; `to_otlp_*` converters map records onto OTLP/JSON
  wire shapes (hex trace/span ids, nanosecond timestamps, kind/status integer codes, typed
  attribute values, sum/gauge/histogram aggregations) and reject any unredacted `SensitiveText`,
  pinning that redaction happens before export, not at the exporter.
- **Context size/compaction metrics come from the PACS-007 seam.** After each successful
  build the runtime reads `last_accounting` through the optional `ContextAccountingSource`
  protocol and emits used/usable token gauges and kept/dropped/compacted item counters;
  builders without the seam are skipped silently.
- **Worker correlation is vocabulary-only.** `CorrelationIds.worker_id` exists (reserved for
  PACS-013); the single-worker runtime leaves it unset.
- **Zero new dependencies.** The entire pipeline is stdlib; `opentelemetry` is not required at
  any layer, keeping domain rule 1 and test rule 9 intact.

## Check

Executed in this environment (2026-08-27, uv-managed toolchain):

- `uv run pytest -q --cov` — **1013 passing, 9 skipped** (skips are pre-existing macOS
  `RLIMIT_AS` platform limits, individually reason-coded); up from 897 passing at PACS-007
- branch-aware coverage — **97.73% overall**; configured 90% gate satisfied;
  `domain/telemetry.py`, `ports/telemetry.py`, and `adapters/telemetry.py` at 100% branch
  coverage; `application/telemetry.py` at 99% (one structural exhaustiveness arc on the final
  `match` case)
- `ruff format --check .` — clean (98 files)
- `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken (plus the AST-based DAG tests:
  domain keeps its stdlib-only import surface)
- deterministic CLI demo — `status=succeeded`, followed by the correlated
  trace/logs/metrics narrative with `[redacted]` visible on the sensitive tool observation
- `compileall` — clean

Acceptance-gate evidence, criterion by criterion:

- *a deterministic demo produces a causally correlated trace/log narrative* — the demo prints
  the run summary plus a trace/logs/metrics narrative in which every span shares the run's
  trace id, cycle spans parent to the root run span, operation spans parent to their cycle, and
  logs/metrics carry run/cycle/action/verification correlation
  (`test_demo_prints_causally_correlated_telemetry_narrative`,
  `test_successful_run_produces_a_causally_correlated_trace`,
  `test_cycle_spans_parent_operation_spans`); equivalence across runs is pinned by
  `test_equivalent_runs_produce_equivalent_telemetry`.
- *secrets/sensitive fields are redacted before export* — unit pins for
  `redact_text`/`redact_attributes`/`redact_record` across all four sensitivity levels, the
  fail-safe boundary redacting before delegation, fail-closed redaction for
  unknown-sensitivity outcomes, OTLP converters rejecting unredacted `SensitiveText`, and the
  end-to-end store-vs-telemetry split test cited above.
- *event store remains the authoritative history and telemetry is explicitly non-authoritative* —
  projection happens only after durable append; no telemetry value is read by any runtime
  decision; the narrative header states the authority split; authority is demonstrated by the
  identical-history fail-safe test and by the full observation surviving in the store while
  telemetry shows `[redacted]`.
- *observability failures cannot corrupt run state* — `FailSafeTelemetry` swallows and counts
  adapter exceptions (parameterized over error types); the integration test runs a full
  successful run against an adapter that throws on every emission; `TelemetrySandbox` emission
  is likewise fail-safe.

## Stop

**SUCCESS.** All four acceptance-gate criteria are demonstrated by executable evidence; every
available quality gate is green. Event history remains the only source of truth: telemetry is a
fail-safe, redacted, non-authoritative projection with no feedback path into runtime state or
decisions.

## Follow-on implications

- PACS-009 can wrap any new sandbox adapter in `TelemetrySandbox` to join sandbox execution
  spans to the run trace without new plumbing.
- PACS-011 live adapters inherit the token/cost/cache accounting surface (`UsageDelta` spans
  and metrics already carry `cached_input_tokens`); live OTel exporters can be added as new
  `TelemetryPort` adapters behind the existing fail-safe boundary, outside deterministic CI.
- PACS-012 routing decisions can emit reason-coded records through the same port; adaptive
  policy still may not expand authority, and telemetry remains unread by runtime decisions.
- PACS-013 can populate `CorrelationIds.worker_id` and add worker-scoped spans without
  changing the vocabulary.
- PACS-015/016 can consume the recorded trace/metric model as the operational projection
  alongside the authoritative event stream for provenance and evaluation.
