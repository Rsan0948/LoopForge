# Completed patch reviews return to repair

The full-a3b reconciliation trial stopped after a valid review report with a real
suspected defect was repeatedly resubmitted. Patch acceptance correctly failed,
but the reviewer remained active, paying for roughly 25 redundant submissions.

`CompletionAwareModel` now stops a patch review before another provider call once
`ReviewTools` has accepted a structurally valid, source-supported report submitted
with complete required coverage. The existing failed-attempt path preserves the
report for the supervisor/coding repair flow. This does not turn blocked reviews
into passing acceptance. Invalid and uncovered reports retain bounded correction.
Completeness is captured at submission, so a later source read cannot retroactively
make an incomplete report terminal before its correction.

Offline regressions exercise actual runtime execution for a suspected finding,
unresolved checks, failing fixed checks, clean acceptance, malformed reports and
reports submitted before coverage. They assert no duplicate report action executes.

The user authorized an additional $0.50 over the previous $1.50 trial. The successor
`review-handoff-2026-10-23` uses a separately frozen $2.00 aggregate ledger bootstrapped
with the predecessor's $1.251884 known spend and zero pending reservations, leaving
$0.748116 for all paid roles. The original policy and ledger remain unchanged.
The latest candidate is snapshotted byte-for-byte; the primary assistant makes no
application edits. Runtime source and the uncommitted correction are frozen and
hashed in the successor artifacts. Journal-based observations record model tools
and their results; they do not claim access to private reasoning.

## Live result

The first launcher encountered stale armed registrations for two explicitly stopped
trials. Those liveness registrations were disarmed without reopening run histories.
The initial successor also inherited `bugfix` intent even though its preserved
baseline passed; this prevented delegation. It was stopped without app edits.
Its six supervisor actions and $0.130638 spending remain accounted for.

The corrected frozen successor is `review-coverage-2026-10-23`, using `feature`
intent for missing regression coverage. All scope, independent checks and sandbox
requirements are retained. Its ledger imports the prior cumulative $1.382522.

This successor reached **delivered**, with verified candidate
`e67b20a7ff4aae569719792fac9044048dc810691e4f83031fe924743b7d7c36`.
Qwen performed 15 proposals: workspace status, one rejected unknown tool request,
source/search investigation and two test edits. Its first edit failed the new
conflict assertion; the second passed every fixed check and independent API,
browser and launcher probe. Gemini performed eight source reads and three report
submissions: the first two were schema-invalid; the third was accepted with one
rejected hypothesis and no unresolved checks. Fresh checks passed and the
supervisor delivered. The primary assistant made zero application edits.

Aggregate spent: **$1.742156 of $2.00**; remaining **$0.257844**; pending exposure
**$0**. The runner is stopped. This demonstrates completion of this preserved
candidate's remaining coverage/review task, not a fresh autonomous app build or
repeated autonomy qualification. The blocked-report exit is pinned by deterministic
runtime regressions; this live review ultimately submitted a clean report.

Verification: full suite 3,266 passed / 67 skipped; final focused 163 passed,
including the two routing cases added after the full suite collected tests.
Pyright: zero errors. Ruff and both import contracts pass.
