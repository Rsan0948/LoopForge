# PACS-014 — Operator command center

Status: SUCCESS

## Objective

Give the operator a durable authority channel and a live console over running
agents: approval-gated tools pause a run on a durable `ApprovalRequested`
until the operator grants or rejects; operator steering/objective amendments
are durable events; a local server projects sessions over REST + WebSocket and
issues operator commands; a React SPA renders the console — without ceding any
runtime authority (the server and GUI are projection + command issuer only;
every run-state change flows through the `Runtime` as durable events).

## Why now

The planned PACS-014 (evaluator + evidence-grounded Reflexion + async HITL,
`docs/process/remaining-pacs-plan.md`) bundles three concerns. The HITL piece
is the smallest, most foundational, and independently valuable: PACS-013 runs
are durable and replayable but not *steerable* — an operator watching a live
repair can only cancel. Approval-gated authority (design principle 6:
high-risk side effects require explicit authority) had a metadata vocabulary
(`ApprovalClass.REQUIRED`) since PACS-001 but no pause/resume machinery and no
production trigger. The evaluator/Reflexion remainder stays deferred.

## Dependencies

- PACS-001–005 (SUCCESS): event-sourced runs, `_ALLOWED_STATUS` transition
  table, compare-and-append SQLite store, budget immutability (rule 11/12).
- PACS-010 (SUCCESS): repair workload, `GitWorkspaceManager` adopted-checkout
  rollback surface (`checkout(paths)`/`reset()`), `WorkspaceArtifactCollector`
  snapshot evidence.
- PACS-011 (SUCCESS): live Ollama adapter; restart-resume safety argument
  (conversational state rebuilds from durable context).
- PACS-013 (SUCCESS): `step()` drive seam (the server drives one cycle at a
  time on per-run driver threads), runs-index precedent (`RunRecord`).
- Operator-authorized addition (2026-09-01): profile-driven `loop` command —
  profiles are the operator-authority vessel the server reuses for session
  wiring (`profile_source` in the session registry).

## Operator decisions (2026-09-01, scope confirmation before any code)

1. **Scope**: operator control + console only. The evaluator and
   evidence-grounded Reflexion portions of the planned PACS-014 are DEFERRED
   to a later cycle; this cycle delivers the authority channel (approval
   gate + durable operator instructions), the serving plane, and the SPA.
2. **Event catalog**: explicit sign-off to grow the frozen schema-v1 catalog
   22→24 — exactly two new event types: `ApprovalRejected` (operator denial
   with reason) and `OperatorInstruction` (steering or objective amendment,
   `amends_objective` flag). `SCHEMA_VERSION` stays 1; all five touchpoints
   (Event union, reducer arm + `_ALLOWED_STATUS`, JSON codec
   registry/constructors, telemetry projector arms, catalog pins) move
   together. `WAITING_FOR_APPROVAL` is reused for the pause — NO new PAUSED
   status.
3. **Dependency relaxation**: fastapi / uvicorn / psycopg enter the runtime
   dependency set, quarantined per ADR-0010; `httpx2` is a dev-only
   type-check dependency (Starlette's TestClient is statically typed against
   it; runtime falls back to `httpx`).

Design decisions recorded during planning (D1–D10): D2 deps per ADR-0010; D3
Postgres adapter mirrors SQLite semantics exactly + docker-compose; D4
`StateStorePort.list_runs` + store schema v2 (runs index, migration
precedent); D5 one driver thread per run + per-run lock, sync runtime never
steps on the event loop; D6 WS streams domain events, fan-out only AFTER
durable append; D7 catalog 22→24 above; D8 `ui/` Vite+React+TS served
statically by the backend; D9 restart rediscovery from the runs index +
session registry; D10 no auth, bind 127.0.0.1.

## In scope

- **M1 — authority channel**: `ApprovalRequested/Granted/Rejected` and
  `OperatorInstruction` reducer arms (approval events must match the pending
  action; requests require a current proposal); runtime `grant_approval` /
  `reject_approval` / `add_operator_instruction` methods; approved-pending
  drive path (a granted run executes exactly the approved action — the
  permission check re-runs, a grant never expands authority); runs-index
  projection (`RunRecord`, incremental fold, SQLite store schema v2 with
  backfill); `PostgresEventStore` with trigger-enforced append-only parity
  and a 12-test conformance suite against live PG; `docker-compose.yml`.
- **M2 — backend**: `FanOutEventStore` (publish only after durable append);
  `SessionManager` (one driver thread per run, bounded pause, driver
  exception recording, registry-persisted wiring, D9 rebuild, workspace-port
  rollback denied while driving); FastAPI app factory (sessions/events/
  artifacts REST, control endpoints, WS history+live stream, 4404 unknown
  run, 404/409/422 mapping, static UI mount last); `loopforge serve` CLI.
- **M3 — GUI core**: `ui/` SPA — sessions list, new-session panel (profile
  picker / validated inline form), session detail with WS-driven live event
  stream (sequence dedupe, reconnect + REST reconcile), approval banner,
  start/pause/resume/stop/instruct controls.
- **M4 — GUI polish**: workspace-snapshot diff viewer (per-file sections,
  add/del coloring, fail-open to raw evidence), selective + full rollback
  UI, objective-amendment dialog (`amends_objective=true`).
- **Production approval gating** (gap found at M3 smoke): `ApprovalGateTools`
  composition decorator + profile `[approval] required_for` — the M1 seam
  had no production trigger because every adapter honestly classifies
  `approval=NONE`; the operator gate is profile/wiring authority.
- **M5 — process**: ADR-0010, this record, HANDOFF checkpoint, ui/README,
  frontend CI job, README quick-start note.

## Out of scope

- evaluator, evidence-grounded Reflexion (deferred, see Operator decision 1);
- authentication/authorization on the server (D10: loopback trusted operator);
- multi-user/remote operation, TLS, non-loopback binds;
- approval policy bindings for `ApprovalClass.POLICY_DEPENDENT` (fails closed
  to the operator until a later cycle);
- budget mutation mid-run (rule 11: adjustment = pause→instruct→resume or
  stop→new run — immutable budgets unchanged);
- Reflexion-driven steering; GUI editing of profiles (profiles stay
  operator-owned TOML on disk).

## Plan

Milestones M1→M5 as scoped above, each gated on §7 (ruff format/check,
pyright strict, import-linter, branch coverage ≥ 90% with the 93.78%
pre-cycle baseline not regressing) and committed separately. Final gate:
the §10 live operator round-trip against real Postgres in Docker and a real
Ollama model.

## Act

Built in five milestone commits plus three fixes surfaced by live smoke:

- `1cfd1ca` M1 authority channel (21 files, +2398/−181; 1527 passed,
  93.87%): events/reducer/codec/telemetry touchpoints, runtime gateway
  methods and approved-pending drive path, runs index, Postgres adapter +
  docker-compose, 15 approval integration tests + 12 PG conformance tests.
- `2e68a7f` M2 backend (9 files, +3269; 1583 passed, 94%): fan-out store,
  session manager, FastAPI surface, serve CLI; 61 new tests incl. full HTTP
  allow+deny and WS history→live→4404 over in-process TestClient.
- `1dd9aa9` fix: `uvicorn[standard]` (a real serve process could not answer
  WS handshakes — TestClient masked it) and accept-before-close so the 4404
  code reaches real ASGI clients (a pre-accept close becomes HTTP 403).
- `34337d3` M3 GUI core (15 files, +3179): two views + controls; tsc strict
  and vite build as UI gates; smoke-verified REST/WS/static against a live
  server with Playwright-driven browser checks deferred to the round-trip.
- `92b4b77` M4 GUI polish (4 files, +524): diff viewer, rollback UI, amend
  dialog.
- `abf9aef` production approval gating (10 files, +252): `ApprovalGateTools`,
  profile `[approval]`, server inline schema, console "gate file writes".
- `38d1f7c` fix: spend approval grants on successful execution (see
  Post-cycle hardening).

Deviations from the earliest sketches (all recorded in commit messages and
module docstrings): session detail objective/status come from the
authoritative replay, not the runs index (the index never tracks objective
amendments); `instruct()` quiesces the driver but does not auto-resume;
`--static-dir` defaults to None; the console inline form omits sandbox
fields (container sessions use profile mode — the realistic operator flow).

## Check

Deterministic gates (all green at cycle close):

- ruff format/check, pyright strict (0 errors), import-linter (2 contracts);
- **1592 passed / 18 skipped / 95% branch coverage** (baseline entering the
  cycle: 1477 / 18 / 93.78%) — includes the 12-test PG conformance suite
  against live Postgres in Docker;
- UI gates: `tsc --noEmit` zero-error, `vite build` clean; new `ui` CI job
  (npm ci → typecheck → build) alongside the Python quality job.

Live operator round-trip (§10 definition of done, 2026-09-02, run
`run_74c650100610`, evidence screenshots in the session log):

1. Session created in the console from an operator profile: local Ollama
   (`devstral-small-2:latest`, economy tier), container sandbox
   (`lf-live-pytest`, PYTHONDONTWRITEBYTECODE to keep the patch-constraint
   surface honest), `[approval] required_for = ["write_file", "edit_file"]`,
   target a scratch git repo with one failing test.
2. The run **paused on the approval-gated write**: `WAITING_FOR_APPROVAL`,
   console banner rendering the exact proposed `edit_file` arguments
   (`return a - b` → `return a + b`).
3. **Approve in the GUI** → durable `ApprovalGranted` → the edit executed in
   the container → verification passed → run `succeeded`.
4. The patch rendered in the **diff viewer** (per-file +1/−1, base revision,
   changed-file inventory).
5. **Selective rollback of one file** from the diff viewer → `calc.py`
   reverted to the base revision (`git status` clean).
6. **Server restart** → session rediscovered and resumed from Postgres: 23
   events replayed, WS stream live, detail projection intact.

## Post-cycle hardening pass

Review ran continuously (parent agent verification of every subagent
milestone, plus live smoke at each step). Findings, all fixed and pinned:

- **Spent-grant re-execution (most severe, found BY the live round-trip)**:
  an approved action that executed successfully but failed verification
  re-planned to READY with the stale proposal projected and the grant
  lingering in `approved_action_ids`; the next cycle re-executed the same
  action at attempt 1, persisting a `ToolExecutionStarted` the reducer
  rejects — durably poisoning the stream (detail/replay 409, driver wedged),
  the same failure class as PACS-011's REFLECTING wedge. Fix: `ToolSucceeded`
  consumes the action's grant; a grant authorizes exactly one completed
  execution, and a re-proposed gated tool re-quiesces the run. Pinned by a
  regression test replicating the live sequence plus a fresh-runtime replay
  poison guard (`38d1f7c`).
- **Missing WS library**: `uvicorn` without `[standard]` cannot answer
  handshakes outside TestClient (`1dd9aa9`).
- **4404 swallowed**: pre-accept close → HTTP 403 on real ASGI servers;
  close moved after accept (`1dd9aa9`).
- **No production approval trigger**: all adapters classify `approval=NONE`;
  the gate is now operator wiring (`ApprovalGateTools` + profile
  `[approval]`, unknown tool names fail closed at bundle build) (`abf9aef`).
- **httpx2 initially questioned, then justified**: removing it broke
  pyright-strict (Starlette's TestClient is TYPE_CHECKING-typed against
  httpx2); kept as a dev-only dependency with the reason recorded in
  pyproject and ADR-0010.
- macOS platform note: local-mode checks can never pass verification on this
  machine (RLIMIT_AS rejected → fail closed); container mode is the live
  path, matching the existing platform-gated test skips.

## Adversarial hardening pass (2026-09-02, three-agent review)

Three parallel adversarial reviews (core runtime/domain/adapters, serving
plane, console UI) plus operator-seeded hypotheses. Every hypothesis was
verified against code before triage; reproductions were executed for all
critical/major findings. Fixed and pinned (allow+deny pairs per rule 10):

- **Permanent-failure grant poison (critical)**: the `38d1f7c` spent-grant
  fix covered only `ToolSucceeded`; a granted action failing PERMANENTLY (or
  exhausting retries) kept its grant, and the re-planned READY state
  re-executed the stale approved proposal at attempt 1 — the identical
  durable-stream poison. Fix: `ToolFailed` consumes the grant too (retries
  never consult the grant, so bounded retries are not starved — pinned by an
  explicit allow test). Defense in depth: `_approved_pending_action` also
  requires `current_attempt == 0`, so an already-attempted proposal can never
  be resurfaced.
- **Stale-grant authority bypass (major)**: the `ActionRejected` arm left the
  grant behind; action ids are adapter-minted (`{run_id}:model-turn-N`) and
  the counter resets on adapter rebuild, so a post-restart proposal reusing
  the id would skip the approval gate entirely. Fix: the grant dies with its
  action; pinned by a reused-id integration test asserting a FRESH
  `ApprovalRequested` and no execution without a second grant.
- **Runs-index objective divergence (major)**: an amending
  `OperatorInstruction` never updated the folded index row; `list_runs()`
  served the stale objective permanently. Fix in the shared fold + a
  conformance pin against full replay.
- **PG backfill race (major)**: the runs-index rebuild was last-writer-wins
  against a concurrent appender (a terminal `RunStopped` could be reverted
  out of the index permanently). Fix: the rebuild takes the same per-run
  `pg_advisory_xact_lock` append holds; concurrent first-initialization is
  serialized by a fixed init lock (plus idempotent trigger DDL). Live-PG
  pins for both.
- **Server lifecycle cleanup (major x2)**: `create_session` leaked the built
  bundle when `runtime.start` failed and left a live-but-unmanaged run when
  `registry.put` failed — now close-on-start-failure and
cancel+close+evict-on-registration-failure. And two sessions could adopt
  the same checkout (racing edits, cross-attributed diffs, mutually
  destructive rollbacks) — one ACTIVE session per resolved repository path
  is now enforced from the durable registry + runs index (restart-safe),
  denied with 409.
- **Error-map completeness**: `StreamVersionConflictError` → 409 (was 500),
  `WorkspaceError` → 422 (rollback of empty/unrevertible paths was a bare
  500), and a new `SessionRegistryError` → 500-with-detail replaces the
  misleading 422/bare-500 split for registry corruption.
- **WS consumer hardening**: history replay offloaded off the event loop;
  a live frame arriving with a sequence GAP (cross-process publish
  reordering) now resyncs from the durable store instead of dropping the
  missed sequence for the life of the connection.
- **Console defects**: SessionView remounts per run (state no longer leaks
  across sessions); out-of-order `getSession` responses are discarded by a
  monotonic `version` guard (stale detail could persist forever on terminal
  runs); NaN/Infinity budget inputs are form errors instead of silently
  serializing to `null` ("no limit"); DiffViewer parses paths with spaces
  and decodes git C-quoted paths.
- **Evidence-document grammar**: untracked-file renders now open with a
  `diff --git` + `new file mode` header (previously headerless difflib
  output merged into the previous tracked file in the viewer, hijacking its
  path and counts) and the workspace diff uses `--no-renames` for parity
  with `status()` (every changed path individually revertible).
- **Misc**: inline TOML environment KEYS are quoted (a newline key could
  inject an extra `[[checks]]` allowlist entry); failed inline profiles are
  validated before their digest file appears in `profiles_dir`; blank
  operator instructions fail closed (they could blank the objective);
  `shutdown()` closes bundles under the per-run lock (never tears down a
  container under an in-flight step); `_ensure_session` loser bundles close
  outside the manager lock; `InMemoryEventStore.append` is one critical
  section; SQLite backfill re-checks inside its write transaction; `serve`
  warns loudly on off-loopback binds.

Accepted limitations (documented, not fixed): `stop()`/`instruct()` park on
the per-run lock behind a wedged in-flight step (state-safe; the bounded
join only bounds the driver exit, by design); registry read-modify-write is
single-process (two servers over one data dir can lose a wiring update);
comma-containing filenames corrupt the snapshot header's display-only file
list (rollback paths come from parsed diff entries, not the header).

## Stop

Cycle closes at 1592 passed / 18 skipped / 95% branch coverage, all §7 gates
green, §10 live round-trip evidence complete. Commits `1cfd1ca`..(M5 docs)
on `main`; local-only workflow per operator instruction (no push).

Post-hardening stop (2026-09-02): the adversarial pass above lands on top of
the closure commits at 1619 passed / 18 skipped / 95% branch coverage
(+27 regression pins), all gates (ruff, pyright strict, import-linter, pytest,
UI tsc+vite) green, live PG integration pins passing against the local
database.

## Follow-on implications

- The deferred evaluator/Reflexion cycle now has its HITL substrate: durable
  approval channel, operator instruction ledger, and a console to bind
  evaluator UI against.
- `POLICY_DEPENDENT` approval still fails closed to the operator; a policy
  binding is the natural next authority-cycle work.
- The codec fail-closes on unknown event types: catalog growth requires
  upgrading all attached binaries together (recorded in ADR-0010).
- `state_for()` full-replay cost is known: the server streams incrementally
  and replays only on demand; a snapshot/projection tier is future work if
  long-running sessions make detail reads hot.
- The GUI inline form cannot express container mode; if operators want
  container sessions without a TOML file, extend the inline sandbox fields
  (the REST schema already supports them).
