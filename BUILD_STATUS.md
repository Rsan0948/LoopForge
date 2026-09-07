# Build status — v1.0 closed (PACS-001…PACS-017)

Open-source readiness re-verified 2026-09-07 on macOS: `uv run pytest -q` —
**2465 passed, 46 skipped, 0 failed** (skips are the macOS `RLIMIT_AS` platform
gates and one non-UTF-8-filesystem gate); operator console served end-to-end
from the seeded demo data (`bash console.sh`).

Historical milestone record (PACS-016):

PACS-016 adds the locked benchmark and multi-trial evaluation laboratory:
runtime policy is now evaluated scientifically — no judging success from
demos or one-off model runs. A code-owned, content-locked 12-category
benchmark fixture set (simple bug, multi-file, misleading failure,
transient API, ambiguous success, context pollution, stale state, stall,
prompt injection, HITL, parallel work, provider outage) is pinned by a
content hash covering fixture bytes and acceptance-hook source; a
task/trial/grader/transcript model makes FALSE_SUCCESS a first-class
verdict; deterministic graders (verified-success, scope, ground-truth,
recovery) grade only durable evidence; trajectory-quality metrics
(repetition, expensive-model use, scope discipline, permission requests,
context efficiency, recovery) project from the authoritative event
stream; a layer-clean multi-trial runner aggregates per-(config, task)
reports with a success/cost/latency/human-intervention Pareto frontier
comparing runtime configurations, never model brands; fault injection is
deterministic code-owned `ModelPort` decorators; reports are
operator-owned artifacts (domain-revalidated on load) exposed read-only
over REST and in the operator console (ADR-0012). Live-model evals stay
in `tests/live/` behind skip probes. The operator-approved
exploration-vs-no-progress fold-in (ADR-0013) skips verification after
successful READ-only turns — exploration no longer accrues stall
strikes — with full replay compatibility (schema v1, 25 events). Cycle
commits `8be4219`..`33165ff`; see
`docs/process/cycles/PACS-016-locked-benchmark-evaluation-laboratory.md`.
No subsequent PACS cycle is active until manually initiated.

Verified in this environment (2026-09-03, Ollama with
`devstral-small-2:latest`, Docker Desktop live, `python:3.12-alpine`):

- `uv run pytest -q --cov` — **2074 passing, 56 skipped** (skips are the
  pre-existing macOS `RLIMIT_AS` platform gates + 1 non-UTF-8-filesystem
  gate; **both live tests executed and passed** — the PACS-011
  Ollama+Docker repair E2E and the PACS-016 12-trial benchmark eval
  matrix; zero credential-gated skips)
- branch-aware coverage — **~94% overall**; configured 90% gate
  satisfied; new laboratory modules at 94–100%
- `ruff format --check .` / `ruff check .` — clean (196 files)
- `pyright` (strict) — 0 errors, 0 warnings
- `lint-imports` — 2 contracts kept, 0 broken
- `cd ui && npm run build` (tsc + vite) — green
- deterministic CI preserved — `--ignore=tests/live`: 2072 passing / 56
  skipped with zero provider credentials
- Linux container full suite (`python:3.14-slim`) — 2069 passing / 35
  environmental skips; every RLIMIT/git-gated benchmark, exploration,
  and eval pin executed
- live eval evidence — bench-simple-bug/misleading-failure/stall ×
  baseline/tight-budget × 2 trials = 12 live container trials, all
  terminal and fully graded, **zero false successes**, both configs on
  the Pareto frontier; tight-budget (`no_progress_limit=2`) failed both
  misleading-failure trials baseline solved — the laboratory's first
  runtime-configuration finding; report persisted and served in the
  console (`output/playwright/m10-eval-detail-live.png`)
- M8 A/B evidence — scripted exploring model on bench-simple-bug and
  bench-stall: baseline (tuned cadence) 2/2 success on both tasks,
  legacy-progress 0/2 (every trial STALLED before the fix landed);
  flailing guards (repeated writes stall, repeated reads bounded) pinned
  in both modes
- benchmark locks pinned — suite lock `e97e6454…` (spec fields), content
  lock `9bc7fe19…` (fixture bytes + hook source, post-M9)

A dedicated two-reviewer adversarial pass (M9) fixed and pinned all 16
confirmed findings before live validation — most severely a content lock
that pinned acceptance-hook identity but not hook source, hook-marker
forgery via crafted filenames in failed verification summaries, and
unguarded evidence reads that could hang or OOM the eval driver on
model-planted paths. Full record in the cycle file's M9 section.

Historical build status (PACS-013 and earlier) is preserved in git
history; HANDOFF.md carries the per-cycle checkpoint narrative from
PACS-001 through the present.
