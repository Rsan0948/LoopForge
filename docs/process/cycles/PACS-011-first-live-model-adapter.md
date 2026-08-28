# PACS-011 — First live model adapter

Status: SUCCESS

## Objective

Integrate one real model provider without giving the provider ownership of LoopForge's
control loop, state, permissions, or stopping decisions.

## Why now

PACS-007 delivered the budgeted `ModelContext` + versioned `RenderedPrompt` contract,
PACS-008 wired the `UsageDelta` accounting surface (previously debited only by scripted
models), and PACS-010 delivered the repair workload with a provider-independent verifier
stack and fixture content already labeled `TrustClass.UNTRUSTED_CONTENT`. Every seam the
live adapter needed existed; what was missing was the adapter, the failure taxonomy, and
the proof that a nondeterministic model can drive the deterministic runtime to a
verifier-granted success.

## Dependencies

- PACS-007 (SUCCESS): context lifecycle/prompt contracts; the adapter consumes exactly
  `ModelContext` + `PromptTemplateRef`, never `RunState` (PACS-006).
- PACS-008 (SUCCESS): usage/cost accounting and telemetry surfaces.
- PACS-010 (SUCCESS): repair workload, verifier stack, container sandbox boundary.

## In scope

- one production-quality `ModelPort` adapter (`OllamaModel`, Ollama native `/api/chat`);
- structured action/output validation and tool-proposal parsing (strict schema, exactly
  one tool call, string-valued arguments);
- token/cost/latency accounting from real provider counts;
- provider failure normalization into a runtime failure-class vocabulary with no provider
  semantics crossing the port;
- model capability metadata (`ModelCapabilities`);
- secret isolation — an optional bearer credential that never enters model context,
  durable events, telemetry, or `repr`;
- live-model tests separated from deterministic CI (`tests/live/`, probe-gated).

## Out of scope

- capability registry and routing (PACS-012);
- multi-provider adapters, streaming, and cloud SDKs;
- evaluator/reflection integration (PACS-014);
- new event types (schema stays v1; model-turn failures terminate through the existing
  `RunStopped` event);
- multi-trial live evaluation (PACS-016): the live gate is a single-shot E2E, and model
  nondeterminism is handled by gating, not statistics.

## Plan

1. Port taxonomy (`ports/model.py`): `ModelFailureClass` (`TRANSIENT`/`PERMANENT` —
   runtime vocabulary, zero provider semantics), `ModelTurnError` (machine `reason_code`
   + bounded human summary), `ModelToolSpec` (code-owned catalog *describing* registered
   tools; rule 4 authority stays on runtime `ToolMetadata`), `ModelCapabilities`.
2. Adapter (`adapters/ollama_model.py`): strict pydantic response validation, HTTP/transport
   failure normalization, per-run conversational message state, honest `UsageDelta` from
   provider token counts, credential confined to request headers. `httpx` added as the
   first HTTP dependency (hermetic tests via `httpx.MockTransport`; rule 9 holds).
3. Runtime failure seam (`application/runtime.py:_drive`): permanent model failures stop
   the run `FAILURE` with the normalized reason code; transient failures retry with
   bounded exponential backoff inside the loop (never burning `max_iterations`, which only
   counts proposed actions) and stop explicitly after the streak is exhausted.
4. Workload catalog (`workloads/repair.py:repair_tool_specs`) describing the full bound
   tool surface with string-valued JSON schemas; entrypoint/CLI wiring with an optional
   `model` dependency that defaults to the scripted model (all pre-existing behavior and
   tests unchanged); `repair-demo --model {scripted,ollama}`.
5. Tests: hermetic unit suite for the adapter, deterministic integration suite for the
   failure seam, and a new `tests/live/` directory whose Ollama+Docker probes mirror the
   `_REQUIRES_DOCKER` pattern so deterministic CI runs with zero credentials.
6. Gates: `ruff format --check`, `ruff check`, `pyright` (strict), `lint-imports`,
   `pytest --cov` with branch coverage ≥ 90%.

## Act

Implemented as planned. Notable decisions and discoveries:

- **The blocker was conversation shape, not prompt wording.** The first two live runs
  stalled: the model read `adder.py`, then re-issued `read_file` every turn until the
  runtime's no-progress stop. A controlled A/B probe against the real server isolated the
  cause: with observations flattened into prompt text, the model treats file content as
  untrusted context and re-reads; with the tool result returned as a structured
  `role:"tool"` message answering its own `assistant.tool_calls`, the *same* model with
  the *same* tools immediately proposes the correct `edit_file` fix. The adapter therefore
  maintains per-run conversation state: each turn's context delta (tracked by
  `(item_id, content)` fingerprint) is translated into provider messages — observation-trust
  items (`DETERMINISTIC_OBSERVATION`, or `UNTRUSTED_CONTENT` after workload demotion)
  answer the pending tool call, everything else arrives as a runtime-update user message.
  State is keyed by run id, never crosses runs, is forgotten on `close()`/
  `drop_conversation()`, and after a process crash simply starts over from the current
  context (safe: the runtime re-drives the loop from durable events). Transient-failure
  retries stay consistent because the seen-set suppresses re-appended deltas.
- **The task objective is code-owned bootstrap authority — use it.** The model's first
  stall was a wrong path guess (`src/adder.py`): nothing in the code-owned objective named
  the workspace layout. `adder_repair_task`'s objective now names `adder.py` and `tests/`
  (what a real repair brief would say), without leaking the solution. Fixture semantics
  and scripted actions are unchanged.
- **Failure normalization is a port-level taxonomy; the domain is untouched.** Adapters
  map provider failures onto `ModelTurnError(failure_class, reason_code)`: 401/403 →
  `MODEL_AUTHENTICATION`, 404 → `MODEL_NOT_FOUND`, 429/5xx/timeouts/connect errors →
  `MODEL_UNAVAILABLE`/`MODEL_TIMEOUT` (transient), other 4xx → `MODEL_REQUEST_INVALID`,
  and any schema/semantic violation of the response (invalid JSON, incomplete generation,
  zero or multiple tool calls, non-string argument values, empty tool name) →
  `MODEL_INVALID_RESPONSE` (permanent). Provider error *bodies* never cross the boundary —
  they can embed prompt fragments or provider internals, so failures carry only the status
  and bounded, control-character-sanitized summaries. The runtime maps permanent failures
  and exhausted transient streaks to `StopReason.FAILURE` with the reason code preserved
  in the durable `RunStopped` summary; no new event type, codec, telemetry, or catalog
  touchpoint was needed (19 event types, schema v1, exhaustiveness pin intact).
- **Usage accounting is real end-to-end.** `BudgetDebited` now carries provider-reported
  `prompt_eval_count`/`eval_count` as input/output tokens; cost derives from
  `ModelCapabilities` per-million-token rates (0.0 for local Ollama — honest, and the
  PACS-008 surface needs no change). Latency accounting rides the existing `MODEL_TURN`
  telemetry span. The live test asserts every debit carries non-zero token counts.
- **Secret isolation is structural, not conventional.** The optional bearer token exists
  only as an `Authorization` header on the adapter's HTTP client: sourced from
  `LOOPFORGE_OLLAMA_API_KEY` at the composition root (never a CLI flag, so it cannot leak
  via process lists), absent from the request body/prompt, from durable events, and from
  `repr` — all pinned by tests. Credential-free operation (plain local Ollama) is the
  default and sends no header.
- **Template contract is enforced.** The adapter renders the domain's versioned
  `PromptTemplate` (`default_controller_template`) and fails closed with
  `MODEL_PROMPT_TEMPLATE_MISMATCH` if the context's `PromptTemplateRef` disagrees — the
  versioned prompt contract from PACS-007 is now load-bearing, not decorative.
- **Validation strictness is semantic, envelope-tolerant.** Provider envelopes may grow
  fields (Ollama adds `index` to function calls); the strict contract applies where it
  matters: exactly one tool call, non-empty name, `StrictStr` arguments (a numeric
  `path: 42` is rejected, not coerced), non-negative token counts, completed generation.

## Check

Executed in this environment (2026-08-27, uv-managed toolchain, Ollama 0.32.15 with
`devstral-small-2:latest`, Docker Desktop 29.2.1 live, `python:3.12-alpine` pulled):

- `uv run pytest -q --cov` — **1281 passing, 15 skipped** (skips are exactly the
  pre-existing macOS `RLIMIT_AS` platform gates and the non-UTF-8-filesystem gate; the
  live Ollama+Docker E2E **executed and passed**, 37s; zero credential-gated skips in this
  environment), up from 1237 passing at PACS-010
- branch-aware coverage — **96.20% overall**; configured 90% gate satisfied
- `ruff format --check .` — clean (130 files); `ruff check .` — clean (0 findings)
- `pyright` (strict mode) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken
- live CLI evidence — `uv run python -m loopforge.entrypoints.cli repair-demo --container
  python:3.12-alpine --model ollama` prints `status=succeeded iterations=2 cost=$0.00`,
  verifier summary `command:run_tests: passed (exit_code=0); patch_constraints: passed
  (files changed: adder.py)`, and the exact unified diff (`return left - right` →
  `return left + right`) recorded as a durable workspace-snapshot artifact — the *live
  model* read the fixture, proposed the patch, and the provider-independent verifier
  granted success

Acceptance-gate evidence, criterion by criterion:

- *a live model completes at least one software-repair fixture through the existing
  deterministic runtime* — `tests/live/test_ollama_repair_live.py`: `OllamaModel`
  (`devstral-small-2:latest`) drives the unmodified PACS-010 repair stack
  (`ContainerSandbox` + `RepairVerifier` + untrusted-content context) to
  `RunStatus.SUCCEEDED`; the assertions require the verifier's passed summary, real
  non-zero token debits, and the exact `+    return left + right` patch in the durable
  artifact stream.
- *malformed provider responses fail explicitly and safely* —
  `tests/unit/test_ollama_model.py` (hermetic `httpx.MockTransport`): non-JSON bodies,
  schema violations, incomplete generations, zero/multiple tool calls, empty tool names,
  and non-string argument values each raise `ModelTurnError(PERMANENT,
  MODEL_INVALID_RESPONSE)`; `tests/integration/test_model_failure_runtime.py` pins the
  runtime mapping — the run terminates `FAILED` with the reason code durable in
  `RunStopped`, resume is a no-op, and no proposal is ever persisted from malformed
  output.
- *model/provider errors map to runtime failure classes cleanly* — the unit suite pins
  every normalization (auth/not-found/request-invalid permanent; 429/5xx/timeout/
  transport transient); the integration suite pins runtime behavior per class: transient
  retries with bounded backoff then proceeds, streak exhaustion stops
  `FAILURE`/`MODEL_UNAVAILABLE` (or the preserved `MODEL_TIMEOUT`), retries never burn
  `max_iterations`, and the streak resets after a successful turn. No Ollama/HTTP
  vocabulary appears outside `adapters/ollama_model.py`.
- *deterministic CI remains runnable with zero provider credentials* — live tests live in
  `tests/live/` behind `_REQUIRES_OLLAMA`/`_REQUIRES_CONTAINER` skipif probes mirroring
  the `_REQUIRES_DOCKER` pattern; with the server or daemon absent they skip with reason
  codes and the remaining 1280 deterministic tests pass untouched (verified via
  `--ignore=tests/live`: 1280 passing / 15 skipped). No credential is required anywhere:
  the default path is credential-free local inference.

## Stop

Post-cycle hardening pass (operator-initiated, 2026-08-28, after the SUCCESS classification):
a three-agent adversarial review of the cycle's own work triaged ~20 findings; every
actionable defect was verified against the real code, fixed, and pinned —

1. **resume from VERIFYING/ACTING into REFLECTING permanently poisoned the event stream
   (most severe):** when a crash/resume landed the run in REFLECTING (verification failed
   on resume), the drive loop persisted `ContextAssembled` without first re-planning to
   READY — an illegal transition durably appended, wedging every future replay of the run.
   Empirically confirmed by the reviewer, then fixed: the drive loop and `resume()` now
   handle REFLECTING by persisting a resume plan (`PlanCreated` is legal in REFLECTING →
   READY) before continuing; pinned end-to-end in
   `tests/integration/test_durable_resume.py` (resume-from-VERIFYING with a failing
   verifier must reach SUCCEEDED and stay replayable across a third process boundary);
2. **provider-exception classification gaps:** `httpx.DecodingError` (malformed gzip /
   undecodable bodies) raises *inside* `client.post()` on httpx 0.28's eager non-streaming
   read and escaped the exception net as an uncaught crash (now normalized to PERMANENT
   `MODEL_INVALID_RESPONSE`); HTTP 408 was misclassified permanent (now TRANSIENT
   `MODEL_UNAVAILABLE` alongside 429/5xx); `TooManyRedirects` and every other
   `httpx.RequestError` subclass are covered by the transport-error net (verified against
   the real httpx 0.28.1 hierarchy: `DecodingError`/`TooManyRedirects` derive from
   `RequestError`, not `TransportError`);
3. **capability/cost validation admitted NaN and infinity:** `float("nan")` cost rates
   passed the `< 0` check and later crashed `UsageDelta` construction mid-turn (now
   `math.isfinite` enforced on both rates); float/bool `context_window_tokens` are rejected
   outright;
4. **trust labels were dropped at the conversational boundary:** observation items
   delivered as `role:"tool"` results were flattened to bare text, stripping the
   `TrustClass` labels PACS-006/010 attach to untrusted workspace content — each line of
   the tool-result content is now prefixed with its trust class, preserving the labeling
   invariant inside the provider conversation;
5. **tool-call parsing rejected legitimate no-arg calls:** a provider omitting the
   `arguments` field (or sending explicit `null`) for a zero-parameter tool like
   `workspace_status` hard-failed the turn (now normalized to `{}` before strict parsing);
6. **adapter constructor validation:** `base_url` now requires an http/https scheme and
   rejects embedded userinfo (credentials in URLs would leak into error surfaces), timeout
   must be finite and positive, temperature finite and non-negative, and a whitespace-only
   `LOOPFORGE_OLLAMA_API_KEY` is treated as absent rather than sent as a bearer token;
7. **unbounded adapter text reached durable events:** `ModelTurnError` summaries flowed
   verbatim into the durable `RunStopped` event, whose fields carry no domain validation —
   stop text is now control-character-sanitized and length-bounded at the runtime boundary
   (`_bounded_stop_text`);
8. **failure-class type confusion:** a plain string `failure_class` bypassed the runtime's
   `is ModelFailureClass.PERMANENT` check, silently downgrading permanent failures to
   transient retries — `ModelTurnError.__init__` now coerces through
   `ModelFailureClass(...)`;
9. **CLI/wiring edges:** `repair-demo --ollama-model ""` died with an uncaught `ValueError`
   traceback (the factory call moved inside the validation `try/except`, exit 2 with a
   clean stderr error), the constructed model client leaked on the error path (now closed),
   the live-test image probe could traceback on missing Docker (`OSError`/
   `TimeoutExpired` handled), and the credential unit test now asserts the `Authorization`
   header is actually applied to outgoing requests, not merely stored.

Findings documented as designed rather than fixed: transient model failures are invisible
across process restarts (the retry streak is deliberately process-local; a resumed run
re-drives from durable events with a fresh conversation); the backoff sleep can overshoot
the elapsed-time budget (consistent with the pre-existing tool-retry sleep semantics); the
per-run conversation history grows append-only, bounded by the iteration/cost control
policies rather than a separate cap; `MODEL_INVALID_RESPONSE` is conservatively PERMANENT
(provider schema drift should stop the run, not retry); model retries share the
`reliability.retry` knob with tool retries (one reliability policy surface); a transient
retry persists a duplicate `ContextAssembled` (idempotent on replay, and deduplicating
would blur the audit trail); and `StrictStr` argument values are the documented contract
(a numeric `path: 42` is rejected, not coerced).

Evidence: **1307 passing / 15 skipped** (the 15 skips are exactly the pre-existing macOS
`RLIMIT_AS` platform gates — 9 local-sandbox, 4 trusted-local repair E2E, 1 CLI smoke —
plus 1 non-UTF-8-filesystem gate; the live Ollama+Docker E2E executed and passed),
**96.35% branch coverage** (90% gate satisfied), `ruff format --check`/`ruff check`/
`pyright` (strict)/`lint-imports` all green, and the live gate re-executed post-hardening
(`tests/live`: 1 passed). Pins live in `tests/regression/test_hardening_regressions.py`
(PACS-011 section), `tests/integration/test_durable_resume.py` (REFLECTING-wedge resume),
`tests/unit/test_ollama_model.py`, and `tests/unit/test_cli.py`.

## Stop

**SUCCESS.** All four acceptance-gate criteria are demonstrated by executable evidence,
including a live model repairing the fixture through the hardened container runtime, and
every quality gate is green with hermetic, credential-free CI preserved. The provider owns
text generation only: proposals are re-authorized against code-owned metadata every turn,
verifier truth stays deterministic, and termination remains runtime-owned. No new ADR was
required: the cycle realizes the PACS-011 scope of `remaining-pacs-plan.md` on
ADR-0005/0008/0009. Checkpoint not yet committed (awaiting operator confirmation).

## Follow-on implications

- PACS-012 (capability registry and routing) consumes `ModelCapabilities` directly; the
  honest next metadata is per-model context windows and cost rates supplied at wiring time
  (the adapter's default window is deliberately conservative).
- The conversational-turn translation (observation-trust items answer pending tool calls)
  is the reference pattern for the next live adapter; a second provider should first
  replicate the A/B probe that pinned the flattened-observation failure mode here.
- Conversation state is process-local: a crashed run resumes with a fresh conversation
  rebuilt from durable context — safe but a mild capability regression. If multi-turn
  fidelity across resume ever matters, the seam is the adapter, not the event schema.
- Live-model cost is currently `0.0` (local inference); when a priced provider is added,
  `BudgetLimit(max_cost_usd=...)` becomes a real control input and the accounting path is
  already load-bearing.
- `tests/live/` is the designated home for live-model evals; PACS-016's multi-trial
  laboratory should extend the same probe-gating pattern rather than inventing a new one.
