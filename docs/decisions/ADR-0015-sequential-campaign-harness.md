# ADR-0015: Sequential campaign coordination outside individual runs

Status: accepted for the experimental local harness.

Long investigations need multiple bounded runs without resetting their evidence
or conflating an accepted model report with verified software behavior. Local
n8n coordinates nested campaign and polling loops. A campaign ledger and reducer
own checkpoint transitions; the existing LoopForge runtime and event reducer
continue to own authoritative run state.

One unfinished campaign and one non-expiring execution slot per installation
prevent concurrent repository and agent work. Crash recovery is explicit and
refuses to release a live worker. Campaign completion and the HelicOps launch
stage require operator evidence. The launch stage also requires a dedicated
branch descended from the pinned baseline.

The investigation executor is strictly read-only and reports only hypotheses. It
registers no mutation or execution tools. Source comes from bounded pinned Git
blobs, and repository/model observations retain lower-trust authority. Model
reports have strict schemas and checked source quotations. Fix and launch
executors have independent operator-owned contracts, dedicated local checkouts,
pre-execution edit confinement and read-only container check mounts. Fresh patch
review and rechecking run sequentially after coding, then stop for acceptance.
The investigation executor cannot acquire that authority from campaign content.

See [campaign harness](../architecture/campaign-harness.md) for deployment,
budgets, recovery, context limits, and coding task operating instructions.
