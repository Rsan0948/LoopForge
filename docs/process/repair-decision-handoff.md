# Observable stateful repair handoff

The specialist handoff already persisted Gemini's diagnosis and reintroduced it in
fresh local contexts. It did not retain the local model's commitment to one repair.
The operator authorized a concise structured decision before more investigation.

`repair_decisions` is a frozen, opt-in campaign setting requiring the existing
fresh-state specialist configuration. Disabled serialization preserves old authority
hashes. The stopped trial is cancelled and archived; no live policy is expanded.

The new `record_repair_decision` action accepts only a target defect, target path,
missing evidence, concrete next action and status. Fields are bounded strings and
the target must satisfy the existing writable contract. It records a brief public
work plan, not private chain-of-thought. Successful records enter the ordinary
domain event stream and are projected back as `MODEL_INFERENCE`, never as runtime
policy or human instructions. Reconstruction works after runtime/database restart.

The code-owned policy permits three investigation actions per unchanged candidate
and specialist advice. Different file reads still consume that allowance. Rewording
a decision or rerunning unchanged checks cannot refresh it. Afterward the worker
must edit, verify or explicitly record inability to proceed. An inability stops the
attempt with an auditable decision; unchanged automatic successors remain blocked.
The original edit, sandbox, budget, circuit and acceptance controls still apply.

The local model's advertised tools are narrowed to the decision action when a
decision is required, and investigation tools are omitted when the allowance is
exhausted. Filtering can only select a nonempty subset of registered tools. An
independent model wrapper rejects forbidden proposals even if a provider ignores
the advertised catalog. No failed proposal executes an action or renews allowance.

A separate deterministic receipt projects an earlier specialist edit only when its
exact proposed patch matches a successful domain tool event. It says the edit was
historically applied; it does not assert the bug is fixed. This prevents historical
diagnosis text from being mistaken for an entirely pending repair.

The successor is `.loopforge/tasks/reconciliation-decisions/`. It preserves model
candidate `7778910c52960fe157172408470c161c05937676` and carries forward the paid
repair ledger (one intervention, $0.038366 spent) and original cancelled-run events.
The primary assistant made no application repairs. Orchestration allowance is the
remaining $0.743734; specialist allowance is the original $0.50 ceiling with its
prior spend deducted by the replayed ledger. Historical Gemini spend is $0.923910,
within the user's $5 authorization. No duplicate specialist call is needed merely
to resume the saved guidance.

Validation: 3,103 tests passed, 67 skipped; final focused tests passed 201 cases,
including the outcome-receipt addition. Ruff lint/format, Pyright and both dependency
contracts passed. Tests cover decision persistence, allowed execution, read-cap and
rewording denial, explicit inability, unchanged retry denial, schema/scope/unjournaled
record rejection and provider catalog narrowing/expansion denial. Pinned preflight
and launch evidence are recorded with the trial's frozen manifest. Harness tests
do not establish application completion.

## Forced best-guess trial

The operator requested a concrete attempted solution rather than another inspection
loop. `force_repair_attempt` is a separate frozen opt-in requiring repair decisions;
it is omitted when disabled to preserve old intake hashes. It requires a nonblank
`best_guess` containing the model's proposed source change. This remains model
inference, not authority or proof. An explicit inability still records its guess
and blocker rather than fabricating a successful repair.

For this mode, two non-edit actions (including checks) consume the allowance. Then
the advertised catalog and independent proposal gate permit only registered source
edit actions or an explicit inability record. Repeated decisions do not refresh the
allowance. Existing writable paths, hashes, edit validation, permissions, sandbox,
budgets and mandatory verification remain authoritative. An attempted or rejected
edit is not evidence of a working solution. Each guard rejection now names the
proposed tool and exact failed rule without persisting private reasoning.

The stopped decision trial is preserved in
`.loopforge/tasks/reconciliation-forced-attempt/stopped-decisions.tar.gz`. The new
trial uses the exact captured application, imports all terminal historical runs
(including the specialist edit receipt), and carries the existing specialist ledger:
one call, $0.038366 charged, no pending reservations. Remaining orchestration budget
is $0.725678, deducting the predecessor's $0.018056. No application fixes were made
by the primary assistant. Its preflight reproduces project/API failures and passes
pinned environment readiness; harness verification does not establish app success.
