# PACS-014b: Operator usability — repo-agnostic session flow

Status: CLOSED (2026-09-02; operator-directed interstitial cycle)

## Objective

Make the operator command center usable against ARBITRARY repositories, not
just the two shipped profiles. The first real operator session (a seeded-failure
blackjack demo repo, 2026-09-02) failed for environmental and product reasons
that had nothing to do with the demo; this cycle fixes those. The demo's success
is a side effect, recorded as live validation evidence.

## Why now

The operator's first repo-agnostic session stalled twice in a row. Root-cause
analysis from the durable event streams (runs `run_362a3623ce07`,
`run_9d2d4828c796`) showed the tool — not the model — was at fault in every
case. PACS-015..017 (provenance, benchmarks, adaptive context) remain PLANNED;
the operator directed this interstitial cycle instead ("making blackjack work
shouldn't be the point; it should be the side effect of fixing these issues").

## Findings (each reproduced from durable events)

1. **macOS local sandbox is a hard wall.** The constrained local sandbox fails
   closed because macOS rejects setrlimit(RLIMIT_AS) — every check execution
   ends in `sandbox launcher failed: resource limits rejected` and the run's
   tests never execute. Container mode (Docker) works, but was only reachable
   via hand-written TOML profiles; the inline form hardcoded
   `container_image: null` even though the REST schema, renderer, and runtime
   wiring already supported it (UI-only gap).
2. **The stall detector was untunable.** `ControlPolicy.no_progress_limit` was
   hardcoded to 3, and the no-progress counter only resets on verification-SCORE
   improvement while every turn (even a read-only one) triggers a full
   verification. A small local model that explores a new repo for 3+ turns
   before its first edit is killed by `STOP_STALLED_NO_PROGRESS` every time —
   exploration is indistinguishable from flailing.
3. **Terminal runs were dead ends.** A stalled/failed run's evidence (stop
   reason, final verification, what the model tried, what changed) died with
   it; the operator had to manually reconstruct context for a retry.
4. **Missing-field errors read like invalid-value errors.** A `[budget]` table
   without `max_cost_usd` failed with "must be a positive finite number".
5. **The live server and the PG integration suite share one database.** During
   this cycle's live validation the full test suite was launched while a real
   session was driving; the suite's `fresh_schema` fixture
   (`DROP SCHEMA public CASCADE`) wiped the live event store mid-run. The
   in-flight driver kept appending to the re-initialized empty schema,
   producing a corrupted half-stream that could not even replay
   (`BudgetDebited is invalid while run is created`) and whose runs-index row
   held the repository claim hostage until manual cleanup. The guard worked
   as designed — it was the OPERATOR (agent) sequencing that was wrong — but
   the suite should not be able to annihilate live sessions by default.

## Operator decisions (2026-09-02)

1. Fix the tool, not the demo: repo-agnostic usability is the cycle goal.
2. Follow-up is MANUAL: a "follow up" action creates a quiescent successor
   session seeded with a consolidated report; the operator reviews the
   objective and presses start. No auto-chaining (runaway-loop risk).
3. The stall threshold becomes an operator knob; the default stays 3 (no
   behavior change without opt-in).

## In scope

- `[budget] no_progress_limit` in TOML profiles + inline form (REST + UI),
  plumbed LoopProfile → RepairRuntimeDeps → ControlPolicy (M1).
- `sandbox.container_image` in the inline session form (M2, UI-only).
- Manual follow-up: deterministic bounded report from the durable event
  stream; `POST /api/sessions/{run_id}/follow-up`; console button (M3).
- Missing-required-number error UX in profile parsing (pre-cycle fix).
- Docs (this record, README, HANDOFF) + live validation evidence.

## Out of scope

- Auto-chaining runs (explicitly rejected — decision 2).
- Changing WHAT counts as progress (score semantics, e.g. not counting
  read-only turns) — a deeper design change, deferred to PACS-015+ planning.
- Orchestrated (multi-worker) no_progress_limit plumbing — no profile path
  exists for orchestrated runs today.
- macOS local-sandbox RLIMIT_AS itself (platform limitation; container mode
  is the supported path, documented).

## Plan

1. Pre-cycle: missing-required-field error + pin.
2. M1: no_progress_limit knob end to end.
3. M2: container image field in the inline form.
4. M3: follow-up endpoint + console button.
5. M4: docs + live validation against the blackjack demo.

## Act

- `fix:` missing required profile numbers report as missing, not invalid
  (`9e5d29f`).
- `feat:` no_progress_limit profile knob (`b11015c`) — profile parse
  allow/deny/default pins, production-factory deps→policy pins, inline REST
  round-trip pin; default 3 unchanged; restart-safe via persisted TOML.
- `feat:` container mode in the inline session form (`5cece3d`) — optional
  image field with helper text; server pin round-trips the image to the
  persisted TOML with an in-container argv (the {python}-in-container
  contract rejection was itself confirmed during the pin).
- `feat:` manual follow-up (`995abd6`) — `entrypoints/followup.py`
  (pure, bounded ≤2000 chars, evidence-only, success/failure-aware closing);
  `SessionManager.follow_up` (terminal-only, managed-only, clones wiring,
  reuses the hardened create path; the repository-exclusivity guard passes
  because the source run is terminal); REST 201/404/409; console button
  navigates to the quiescent successor.

## Check

Gates: ruff format/check clean, pyright 0 errors (strict), import-linter
2 contracts kept, UI tsc + vite build green. Full suite at cycle close:
**1634 passed / 18 skipped (platform gates) / 95% branch coverage**
(baseline entering the cycle: 1619 passed; +15 pins).

Live validation (operator machine, Ollama `devstral-small-2:latest`,
Postgres in Docker, container image `blackjack-checks:local`):

- M3 dogfood: `POST /api/sessions/run_9d2d4828c796/follow-up` on the stalled
  container run → `run_dffa8c4cfe67`, READY and quiescent, objective =
  original task + report carrying the real stall evidence (stop summary,
  4 iterations, final verification text, tool counts, empty workspace
  inventory). VERIFIED 2026-09-02. (This successor was later destroyed by
  the schema-drop incident in finding 5; its seeded objective was verified
  before the loss.)
- M1+M2 end-to-end: fresh session `run_0050e23e9d46` from the blackjack
  profile (container mode, `no_progress_limit = 10`) drove to
  **STOP_SUCCESS_VERIFIED in 11 turns with 3 approval-gated edits** —
  including SIX consecutive read-only exploration turns before the first
  edit, which the hardcoded threshold of 3 had made unsurvivable. The
  landed diff restored ace demotion, the dealer 17 threshold, and the 3:2
  payout; 10/10 tests green; committed in the demo repo as `9c820e7`.
  VERIFIED 2026-09-02.

## Stop

Cycle closes 2026-09-02 at 1634 passed / 18 skipped / 95% branch coverage,
all gates green, live validation evidence complete (M3 follow-up creation
verified; M1+M2 container run to STOP_SUCCESS_VERIFIED verified). Commits
`9e5d29f`..(docs) on `main`; local-only workflow per operator instruction
(no push).

## Follow-on implications

- Read-only exploration turns burn full verification runs AND no-progress
  strikes. A progress model that distinguishes "gathered new information"
  from "no progress" (or defers verification to workspace-changing turns)
  is the natural PACS-015-era design question.
- The inline form still cannot express `environment`, `max_memory_bytes`,
  or `local_python` overrides; add fields if operators need them without
  TOML files.
- Follow-up is one-hop and manual by design; chain analytics (run lineages)
  belong to the PACS-015 provenance graph.
- The PG integration fixture should use a dedicated test database (or refuse
  to run against a DSN a live server is using); today `DROP SCHEMA public
  CASCADE` on the default DSN annihilates any live server's sessions.
- No cross-process liveness marker exists for runs: a server that dies
  mid-drive leaves a non-terminal runs-index row holding the repository claim
  (observed with the corrupted zombie run above; `stop` could not even replay
  the half-stream). A claim-expiry or force-release path is future work.
