# ADR-0020: Clarification experiments and durable local goal review

Date: 2026-10-20  
Status: implemented; bounded Phase 4 container and browser qualification

## Decision

Keep clarification authority in trusted intake. `clarifications` supplies a topic,
question, classification, timeout, and (only for `optional`) an already authorized
fallback. Opening the question persists its absolute deadline in the append-only
goal journal. A model cannot classify its own permission request or grant itself
a fallback. Permission answers require a new frozen intake; answering an existing
question never expands the current goal's scope, checks, budget or sandbox.

Scoped inspection, environment preparation and baseline reproduction can continue
while waiting. Dependent delegation, retry, verification and delivery remain
denied. Once independent preparation finishes, the runner waits without spending
inference calls. Missing-information and permission timeouts never start an
experiment. The hard goal deadline still applies.

An optional timeout atomically records the assumption, deterministic experiment
branch and authorized checkpoint. The existing worker intent ledger and checkout
reconciliation create that branch when coding begins. Repeated steps and restarts
reuse the intent and branch. Branch names are resolved by code, and all original
worker authority and independent acceptance criteria remain mandatory.

A conflicting late answer requests a pause, invalidates a returning supervisor
decision, and prevents future acceptance. Completed work stays available. Terminal
goals remain terminal; late answers and disposition only annotate the result.
A new goal is required to pursue conflicting direction. Matching late answers
retain the candidate's eligibility for explicit review.

## Dirty source capture

`capture_working_tree: true` explicitly includes tracked and nonignored untracked
working bytes in a separate snapshot repository. It records the original commit,
actual snapshot commit, staged diff hash, file hashes/modes and ignored exclusions.
Deletions and executable modes survive. The original index, branch and files are
preserved; optional Git index refresh writes are disabled. Capture compares source
state before and after copying and fails if it changed. Symlinks, submodules,
special files, non-UTF8 paths and oversized snapshots fail closed. An incomplete
snapshot is retained for inspection and requires a fresh goal directory.

The snapshot becomes the frozen investigation and worker checkpoint. Existing
Phase 3 intake digests remain readable when new options are unused. A Git clone
and experiment branch separate changes; they do not provide execution isolation.
Container capability preflight remains mandatory.

## Controls and review

Pause/cancel requests can be journaled while the runner owns its service lock.
They are visibly **requested** until an in-flight provider/worker result has been
reconciled. Responses still settle their budget reservation, including full
reservation charges for interrupted unknown inference. CAS retries preserve
concurrent operator updates. A pending cancellation cannot be downgraded by a
late answer. A delivery racing a control request acknowledges the control instead.

The CLI stays alive while paused; resume clears the pause without resetting the
deadline or spending allowance. Acceptance/discard require a terminal result and
exclusive local service ownership. Acceptance rechecks the current candidate and
independent receipt, then commits only the candidate branch. Git commit recovery
is naturally idempotent and requires a clean candidate; an older commit with new
uncommitted edits is rejected. The goal disposition is journaled after success.
Operator answers are serialized with acceptance. Discard records a disposition
and retains candidate files rather than deleting them.

The console adds server-owned goal listing, current state, assumptions, deadlines,
cost/reservations, checkpoint lineage, diff/drift, review controls and morning
reports. Mutations require an explicit operator header and matching browser
origin. Goal IDs cannot select arbitrary filesystem paths or symlink directories.
No external notification channel is configured.

## Qualification and scope

Network-free tests exercise restricted silence, approved optional timeout,
independent research, early/late answers, terminal invariants, concurrent controls,
CAS races, exact index preservation, symlink and checkpoint-drift rejection,
acceptance recovery, API origin checks and path confinement. The real-container
multi-file fixture runs the original Phase 3 path plus dirty-snapshot experiment
acceptance and discard, with restart and unchanged source assertions.

`scripts/qualify_goal_review.py` additionally leaves a delivered fixture and JSON
morning report under `.loopforge/qualification/phase4/`. Its provider transport
and coding driver are scripted; readiness, trusted behavioral probes, fresh review,
Git branches, events and receipts are real. Its injected token-accounting amounts
are test values, not provider spend. It makes zero paid provider calls.

Browser qualification opened the actual console, inspected the report and diff,
submitted a matching late answer, accepted the isolated fixture, and confirmed
both buttons became disabled and the accepted commit appeared. The browser
reported zero console errors or warnings. Screenshots remain under
`output/playwright/`.

This phase supports one clarification topic, one experiment branch and one
narrowed assignment per goal. Questions/fallbacks are configured in intake;
arbitrary model-generated clarification topics are not a runtime capability.
Conflict resolution uses a new goal, rather than mutating an existing contract.
Acceptance commits locally and never merges into the user's working branch.
Broad model comparison, arbitrary environment solving, and production overnight
rollout remain Phases 5 and 6. No production service or campaign was restarted.

Final validation: **2,996 passed, 67 skipped, 91% coverage** across the full
regression suite. Skips are unavailable optional services/models, unsupported
host resource isolation and non-UTF8 filesystem cases; unsupported isolation did
not fall back to weaker execution. Ruff, formatting, strict Pyright, both import
contracts, UI TypeScript/Vite build and `git diff --check` passed.

A final targeted container recovery case additionally interrupts acceptance after
the worker commit but before the goal event, moves candidate HEAD, and retries.
The goal records the original commit from a strictly validated, immutable worker
receipt. It never labels the later HEAD as the verified accepted commit. The
receipt/review layer also supports Phase 2 prepared goals. Both original Phase 3
live ledgers replay successfully with their frozen intake bindings unchanged.
No additional paid Gemini call was made; the previous cumulative estimate remains
$0.114208 of the authorized $5, with the existing $2.50 maximum preallocated live
evaluation envelopes unchanged.
