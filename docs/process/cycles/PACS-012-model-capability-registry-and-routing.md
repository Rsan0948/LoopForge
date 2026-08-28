# PACS-012 — Model capability registry and routing

Status: SUCCESS

## Objective

Route by required capabilities, task/risk characteristics, execution state, and budget
rather than hard-coded provider names.

## Why now

PACS-011 delivered the first live `ModelPort` adapter with honest `ModelCapabilities`
metadata and a normalized failure taxonomy — but exactly one provider exists, the
adapter's default context window is deliberately conservative (honest per-model metadata
has nowhere to land), and the model is hard-wired per run (`--model {scripted,ollama}`).
The runtime's drive loop already exposes every routing signal at the model call site
(`consecutive_no_progress` stalls, budget consumed, failure classes, verification score),
and the PACS-008 telemetry surface explicitly anticipated reason-coded routing records.
The seams are all present; what is missing is the registry, the tier vocabulary, and the
routing policy itself.

## Dependencies

- PACS-011 (SUCCESS): `ModelCapabilities` seed type, `ModelFailureClass`/`ModelTurnError`
  taxonomy, per-run conversational adapter state (the reference pattern for any second
  provider), `tests/live/` probe-gating pattern.
- PACS-008 (SUCCESS): closed telemetry vocabularies and the fail-safe emission boundary
  for reason-coded routing records.
- PACS-007 (SUCCESS): `ModelRole` vocabulary (controller/planner/reflector) — routing may
  be role-scoped; only CONTROLLER is exercised by the runtime today.
- PACS-001–005 (SUCCESS): `ControlPolicy`/`BudgetLimit` immutability precedent — routing
  policy sits alongside, never inside, control authority (AGENTS.md rule 12).

## In scope

- **Provider/model capability registry** (`ModelRegistry`): code-owned,
  construction-validated (duplicate provider/model registration fails at construction),
  fail-closed lookup, mirroring the `CompositeToolExecutor` dispatch-registry precedent.
  This is where honest per-model metadata (context windows, cost rates) supplied at
  wiring time lands — resolving the deliberately conservative `OllamaModel` default.
- **`ModelCapabilities` widened onto `ModelPort`** (mirroring
  `SandboxPort.capabilities`): every adapter, including `ScriptedModel`, honestly
  advertises capabilities; capability matching fails closed structurally rather than
  by convention.
- **Model-tier vocabulary** (domain-owned strength/cost classes) and **capability
  requirements** vocabulary (mirroring `SandboxRequirements`: required capabilities a
  task/role needs — e.g. tool-call support, minimum context window).
- **Deterministic routing policy** consuming `RunState` signals (iteration, stalls,
  budget consumed, failure classes, verification score):
  - *vertical routing* between model strengths/cost classes (escalation on stalls,
    failures, complexity, budget pressure);
  - *horizontal provider fallback* (transient failure on one provider routes to a
    compatible alternative);
  - every route/escalation emits a **machine-readable reason code**.
- **Runtime integration**: the drive loop consults the routing policy to select the
  model per turn; mid-run model swap is supported (a swapped-in adapter starts a fresh
  conversation — safe, because the runtime re-drives from durable context; the existing
  PACS-011 resume semantics apply unchanged).
- **Routing telemetry**: routing decisions projected through the existing PACS-008
  surface (closed vocabulary extension only — no new domain event types; schema v1,
  19 events unchanged).
- **Entrypoint/CLI wiring**: the registry replaces the `--model {scripted,ollama}`
  if/else; honest per-model capability metadata is supplied at wiring time.
- **Tests**: routing policy tested entirely with fake adapters (acceptance gate);
  capability-mismatch fail-closed proofs; reason-code pins; telemetry projection;
  runtime mid-run swap integration; all quality gates green.

## Out of scope

- a second live provider adapter (the registry is proven with fakes + scripted + the
  existing Ollama adapter);
- statistical/learned routing, shadow policies, or any adaptation from execution
  history (PACS-017);
- orchestrator/worker routing (PACS-013);
- new domain event types (routing decisions are telemetry, not events);
- changes to `ControlPolicy`, `BudgetLimit`, permissions, or any immutable-during-run
  authority — routing may never expand runtime authority (rule 12);
- multi-trial live evaluation (PACS-016).

## Plan

1. **Domain vocabulary** (`domain/routing.py`): `ModelTier` (strength/cost classes),
   `ModelRequirements` (fail-closed capability requirements a role/task declares,
   mirroring `SandboxRequirements`), `RouteReason` closed machine-code vocabulary,
   `RoutingDecision` artifact (selected provider/model/tier + reason code). Pure domain
   — zero provider semantics.
2. **Port widening** (`ports/model.py`): `ModelPort` gains `capabilities`; `ScriptedModel`
   gains honest capabilities (scripted provider, zero cost, declared window); the
   existing `OllamaModel` property now satisfies the port structurally.
3. **Registry port + adapter** (`ports/routing.py`, `adapters/model_registry.py`):
   registration entries bind an adapter to its capabilities/tier; duplicate
   provider/model registration fails at construction; lookup by requirements fails
   closed (`NoCompatibleModelError` vocabulary).
4. **Routing policy adapter** (`adapters/routing.py`): deterministic
   `TieredRoutingPolicy` with a frozen, validated config (escalation thresholds,
   budget-pressure fraction); vertical escalation on stalls/failures/budget, horizontal
   fallback across providers at an equal-or-better tier; every decision reason-coded.
5. **Runtime integration** (`application/runtime.py:_drive`): per-turn model selection
   through the routing policy; mid-run swap (fresh adapter conversation — safe under
   existing resume semantics); routing failure (no compatible model) stops the run
   `FAILURE` with the reason code through the existing `RunStopped` path (no new event
   types); hard budgets and permission policy untouched (rule 12).
6. **Telemetry**: routing decision span attributes/metrics through the closed PACS-008
   vocabulary (policy-decision surface), keeping telemetry non-authoritative.
7. **Wiring**: `entrypoints/repair.py` + `entrypoints/cli.py` build the registry with
   honest per-model metadata supplied at wiring time; scripted default unchanged.
8. **Tests**: unit suites for domain vocabulary, registry, and routing policy (fakes
   only); capability-mismatch fail-closed; reason-code pins; telemetry projection;
   runtime mid-run-swap integration; existing suites updated for the widened port.
9. **Gates**: `uv run pytest -q --cov` (branch ≥ 90%), `uv run ruff format --check . &&
   uv run ruff check .`, `uv run pyright` (strict), `uv run lint-imports`.

## Act

Implemented as planned. Notable decisions and discoveries:

- **Capability vocabulary moved home to the domain.** `ModelCapabilities` moved from
  `ports/model.py` to `domain/routing.py`, mirroring the `SandboxCapabilities`
  precedent exactly: capability and requirements vocabulary is code-owned domain
  knowledge, ports declare it structurally, adapters advertise it honestly. Alongside
  it: `ModelTier` (ECONOMY/STANDARD/ADVANCED strength/cost classes with an explicit
  rank), `ModelRequirements` (fail-closed contract — tool-call support, minimum
  context window, optional per-million-token cost ceilings — with a `missing_for`
  matcher returning requirement names), the closed `RouteReason` machine-code
  vocabulary, and a validated `RoutingPolicyConfig` that documents its own lack of
  authority (rule 12).
- **`ModelPort` widened structurally.** The protocol now declares a `capabilities`
  property (mirroring `SandboxPort.capabilities`); `ScriptedModel` gained honest
  default capabilities (scripted provider, the runtime's wired 4096-token context
  budget declared as its window rather than an implied unbounded capacity, zero cost
  rates) and accepts wiring-supplied overrides. All pre-existing test fakes gained
  capabilities — the widening is enforced by pyright strict across tests.
- **The registry is wiring-time authority.** `ModelRegistry`/`ModelRegistryEntry`
  (`adapters/model_registry.py`) mirror the `CompositeToolExecutor` precedent:
  duplicate provider/model registration fails at construction, entry capabilities are
  boundary-validated at construction (adapters may violate port types), tiers are
  enum-coerced, `candidates()` fails closed on requirements, and `entry_for()` raises
  `ModelLookupError` on unknown identities. This is where honest per-model metadata
  supplied at wiring time lands — the CLI now builds `ModelCapabilities` for the
  Ollama adapter explicitly (`--ollama-context-window`, default 131072), replacing
  the adapter's deliberately conservative default.
- **Tier semantics survived a design test.** The first policy draft selected the
  cheapest candidate *across all equal-or-stronger tiers*, which let a free ADVANCED
  model silently win the initial STANDARD selection — vertical movement happening as
  a side effect of pricing rather than as an explicit escalation decision. The
  policy now selects the cheapest entry *at the target tier exactly*, climbing only
  when that tier has no compatible candidate; escalation and budget de-escalation
  are the only vertical moves, both reason-coded.
- **Runtime integration is per-turn and authority-free.** The drive loop gained an
  optional `router`: each cycle selects the model through `RoutingPolicyPort` with
  `RoutingSignals` derived from authoritative state (`consecutive_no_progress`, the
  loop-local transient failure streak, a fallback flag, and a *read-only*
  remaining-budget fraction computed from the code-owned `BudgetLimit` — routing
  reads budget context but enforcement stays entirely with `ControlPolicy`). The
  first turn routes with no current model (`ROUTE_INITIAL_SELECTION`); mid-run swaps
  are safe because a swapped-in adapter starts a fresh conversation from durable
  context, exactly like the PACS-011 crash/resume path. A model-less decision stops
  the run `FAILURE` with `ROUTE_NO_COMPATIBLE_MODEL` durable in the existing
  `RunStopped` event — schema v1, 19 event types, no new events. Transient-failure
  retry semantics are unchanged: the fallback flag simply lets the router offer a
  horizontal alternative before the bounded backoff retry (`FALLBACK_UNAVAILABLE`
  honestly retains the current model when no equal-or-stronger alternative exists).
- **Routing telemetry rides the PACS-008 surface.** One closed-vocabulary extension:
  `SpanName.MODEL_ROUTE` (`loopforge.model.route`) with
  `loopforge.route.reason_code/provider/model/tier` attributes, emitted per routed
  turn; router-less runs emit nothing and every pre-existing span pin is unchanged.
- **Precedence is documented and pinned.** Budget pressure beats stall escalation
  (routing may bias cost downward, never upward past a stall signal); retention
  beats everything (`ROUTE_RETAINED_CURRENT` preserves adapter conversation state
  when nothing demands a change, and is also the honest answer when an escalation
  or fallback has no compatible candidate).
- **The repair workload declares its model contract like its sandbox contract.**
  `REPAIR_MODEL_REQUIREMENTS` (tool calls, 4096-token minimum window) sits next to
  `UNTRUSTED_REPAIR_REQUIREMENTS` in `workloads/repair.py`; `entrypoints/repair.py`
  registers the wired model (scripted ECONOMY default, live models STANDARD via
  `RepairRuntimeDeps.model_tier`) and routes every turn through
  `TieredRoutingPolicy`. Tests that previously monkeypatched `runtime.model` now
  pass models through the code-owned `deps.model` seam.

## Check

Executed in this environment (2026-08-28, uv-managed toolchain, Ollama 0.32.15 with
`devstral-small-2:latest`, Docker Desktop live, `python:3.12-alpine` pulled):

- `uv run pytest -q --cov` — **1357 passing, 15 skipped** (skips are exactly the
  pre-existing macOS `RLIMIT_AS` platform gates + 1 non-UTF-8-filesystem gate; the
  live Ollama+Docker repair E2E **executed and passed** through the now-routed
  runtime; zero credential-gated skips), up from 1307 passing at PACS-011
- branch-aware coverage — **96.47% overall**; configured 90% gate satisfied;
  `domain/routing.py`, `ports/routing.py`, `adapters/model_registry.py`, and
  `adapters/scripted.py` at 100% (`adapters/routing.py` 99%: one defensive
  unreachable-tuple branch)
- `ruff format --check .` / `ruff check .` — clean (139 files)
- `pyright` (strict mode, src + tests) — 0 errors, 0 warnings
- `lint-imports` — 2 architecture contracts kept, 0 broken (routing vocabulary in
  `domain`, policy port in `ports`, registry/policy in `adapters`, wiring in
  `entrypoints` — the layer DAG holds)
- live CLI evidence — `repair-demo --container python:3.12-alpine` (scripted model
  routed through the single-entry registry) → `status=succeeded iterations=2
  cost=$0.02`, verifier summary and exact patch evidence unchanged from PACS-010/011

Acceptance-gate evidence, criterion by criterion:

- *routing policy can be tested entirely with fake adapters* —
  `tests/unit/test_routing_policy.py` (23 tests) exercises the full policy surface
  with `ScriptedModel`-based fakes only: initial selection, retention, stall
  escalation, budget de-escalation and its precedence, horizontal fallback and its
  refusals, tier-empty climbing, determinism; no network, no provider.
- *a task requiring an unsupported capability cannot be routed to an incompatible
  model* — `ModelRequirements.missing_for` matching is fail-closed at the registry
  (`candidates`); the runtime maps a model-less decision to an explicit `FAILURE`
  stop with `ROUTE_NO_COMPATIBLE_MODEL` durable in `RunStopped`, the incompatible
  model is never invoked, and resume is a no-op
  (`tests/integration/test_routing_runtime.py::
  test_task_requiring_unsupported_capability_is_never_routed`, plus registry and
  requirements unit pins).
- *every route/escalation emits a machine-readable reason code* — the closed
  `RouteReason` vocabulary (7 codes) is carried on every `RoutingDecision`
  (enum-coerced, invariant-validated) and projected onto the
  `loopforge.model.route` span with provider/model/tier attributes; integration
  tests pin the exact reason-code sequences for initial selection, retention,
  escalation, de-escalation, fallback, and fallback-unavailable runs.
- *hard budgets and permission policy remain outside adaptive routing authority* —
  `RoutingPolicyConfig` holds no budget limit; the runtime computes a read-only
  remaining-budget fraction from the code-owned `BudgetLimit` and routing only
  reads it to bias tier selection downward. `ControlPolicy.evaluate` runs before
  every routed turn exactly as before; budget-exhaustion and stall stops in the
  routed integration runs fire through the unchanged control path. Permission
  policy is untouched (rule 12).

Deterministic-CI preservation: with `--ignore=tests/live` the suite is 1356 passing
/ 15 skipped with zero provider credentials; router-less `Runtime` construction
emits no routing telemetry and behaves byte-identically to PACS-011 (all
pre-existing span pins unchanged).

## Post-cycle hardening pass

Operator-initiated adversarial hardening (three-agent review, 2026-08-28, after the
SUCCESS classification): ~15 findings triaged across three lenses (routing core,
runtime seam, wiring/tests); every actionable defect was verified against the real
code, fixed, and pinned in `tests/regression/test_hardening_regressions.py`
(PACS-012 section) —

1. **same-provider "fallback" was possible (most severe semantic gap):** the
   fallback filter excluded only the current model *instance*, so a transient
   provider outage could "fall back" to a sibling model on the same dead provider
   while telemetry claimed `FALLBACK_TRANSIENT_FAILURE` — fallback now requires a
   *different provider*, matching every docstring and the cycle plan;
2. **`default_tier` above all registered tiers wedged runs non-terminal (most
   severe operational defect):** `_cheapest_at_or_above` raised an uncaught
   `RuntimeError` on initial selection — no durable stop, and every `resume()`
   re-raised. The default tier is a *preference* and is now clamped to the
   strongest compatible tier the registry holds (`ModelRequirements` remains the
   hard gate); pinned at policy level and end-to-end (the run succeeds instead of
   wedging);
3. **first selections could be mislabeled `ESCALATED_STALL`/
   `DEESCALATED_BUDGET_PRESSURE`:** on resume-shaped state (`active_model=None`,
   `consecutive_no_progress` already past threshold) the *initial* selection
   claimed an escalation nothing preceded — corrupting the reason-code audit
   vocabulary. Initial selections now always carry `ROUTE_INITIAL_SELECTION`
   (signals still shape which tier the first selection lands on);
4. **no-op budget pressure suppressed stall escalation:** pressure already at the
   cheapest tier blocked the `elif` escalation branch; pressure now takes
   precedence only when it actually moves the target downward;
5. **`RepairRuntimeBundle.close()` closed only `runtime.model` (latent leak):**
   routed turns are served by registry entries, so a multi-entry registry would
   leak a routed live adapter's HTTP client — `close()` now fans out over every
   registered model (deduplicated) and its docstring owns the contract;
6. **`OllamaModel` capability/request identity divergence:** the payload sends
   `capabilities.model`, but a caller-supplied capabilities object was never
   cross-checked against the `model` constructor argument — a wiring typo would
   silently query a different model. Identity (provider + model) is now enforced
   at construction;
7. **contract-validation normalization:** `RoutingSignals.current_model` rejects
   non-`ModelCapabilities` values at the boundary; `ModelRegistryEntry` and the
   runtime's routing boundary normalize missing/ill-shaped adapter surface (raw
   `AttributeError` → `TypeError`/`ModelContractError`, including a
   `propose_action` shape check); `RoutingDecision` now rejects
   `ROUTE_NO_COMPATIBLE_MODEL` carrying a model (self-contradictory audit record);
8. **wiring consistency:** the live acceptance test registered the Ollama model
   at the default ECONOMY tier while the CLI registers STANDARD — aligned to
   STANDARD; `Runtime.router`'s field comment now documents that post-construction
   `model` mutation has no effect once routing owns selection (the behavior change
   that the cycle's test migration already proved).

Findings documented as designed rather than fixed: the transient failure streak
and fallback flag are process-local across crash/resume (the PACS-011 documented
posture — resume re-drives from durable events with a fresh
`ROUTE_INITIAL_SELECTION`, which may re-try a just-failed provider once before
falling back); post-resume tier re-derivation from `default_tier` (durable model
identity would require a schema change — out of scope under no-new-events);
router adapter exceptions fail loud uncaught (consistent with the existing
`ModelContractError`/`ContextContractError` boundary posture); model identity is
telemetry-only by design (no new event types — `BudgetDebited`/`ActionProposed`
carry no provider attribution); route spans close `OK` on fail-closed decisions
(routing succeeded; the *decision* stops the run — consistent with the
pre-existing CYCLE span pattern); ollama CLI flags are silently ignored under
`--model scripted` (established argparse pattern); the repair requirements ↔
context-budget 4096 constant is duplicated across two files by design (each side
is independently validated); and the macOS trusted-path `repair-demo` traceback
is pre-existing platform fail-closed behavior, verified byte-identical on the
PACS-011 checkpoint.

Post-hardening evidence: **1370 passing / 15 skipped** (skips unchanged; the live
Ollama+Docker E2E re-executed and passed at the corrected STANDARD tier),
**96.71% branch coverage** (90% gate satisfied; `domain/routing.py`,
`ports/routing.py`, `adapters/model_registry.py`, `adapters/scripted.py` at
100%), `ruff format --check`/`ruff check`/`pyright` (strict)/`lint-imports` all
green. Pins live in `tests/regression/test_hardening_regressions.py` (PACS-012
section, 13 tests) plus alignment fixes in `tests/unit/test_ollama_model.py`,
`tests/unit/test_model_registry.py`, and `tests/live/test_ollama_repair_live.py`.

## Stop

**SUCCESS.** All four acceptance-gate criteria are demonstrated by executable
evidence, every quality gate is green, the live Ollama+Docker repair E2E passes
through the routed runtime, and deterministic CI remains hermetic and
credential-free. Routing owns model selection only: it cannot touch budgets,
permissions, verification truth, or stopping, and it fails closed — never silently —
when no compatible model exists. The operator-initiated post-cycle hardening pass
(above) fixed and pinned every actionable finding. No new ADR was required: the
cycle realizes the PACS-012 scope of `remaining-pacs-plan.md` on the ADR-0005
port/adapter pattern.

## Follow-on implications

- PACS-013 (orchestrator/worker) can route per worker role: `RoutingSignals` is
  per-turn and `ModelRequirements` is role-shaped; a PLANNER/REFLECTOR requirement
  set is a wiring-time decision, not a runtime change.
- A second live provider should register beside Ollama with honest capabilities —
  the registry is the seam — and should first replicate the PACS-011 A/B
  conversational probe; horizontal fallback will then be exercisable live, not only
  with fakes.
- When a priced provider arrives, cost ceilings in `ModelRequirements` and
  budget-pressure de-escalation become load-bearing control inputs; the accounting
  path (`ModelCapabilities` rates → `UsageDelta` → `BudgetDebited`) already carries
  real rates end-to-end.
- PACS-017 (adaptive/shadow policies) gets its routing hook: `RoutingPolicyPort` is
  the seam a shadow policy can observe without enacting, and the reason-coded span
  stream is the audit trail for comparing candidate policies.
- Mid-run model swaps start a fresh adapter conversation (safe under PACS-011
  resume semantics) but lose conversational fidelity; if that ever matters, the
  seam is conversation handoff in the adapters, not the event schema.
