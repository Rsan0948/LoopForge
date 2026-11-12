# Prepared goal supervisor

Phase 2 provides an opt-in CLI over a separate ledger. Existing production
campaigns and pending tasks are not changed.

Use `.venv/bin/python -m loopforge.entrypoints.supervisor` (or the installed
`loopforge-supervisor` command). The commands are:

```sh
loopforge-supervisor create --config prepared-goal.json --directory .loopforge/my-goal
loopforge-supervisor run --directory .loopforge/my-goal --key-file .loopforge/secrets.env
loopforge-supervisor status --directory .loopforge/my-goal
loopforge-supervisor cancel --directory .loopforge/my-goal
```

`create` consumes a strict `PreparedGoalConfig`: `campaign` and `task` use the
existing operator schemas; `max_cost_microusd` is required (at most 5,000,000).
`max_goal_seconds`, `max_decisions` and `max_failures` bound the entire goal.
The task requires explicit changed-file deliverables and trusted acceptance
probes. Pin an existing container image; no image pull or host fallback occurs.
A prepared goal starts from the explicitly named commit. Dirty user snapshots
and generalized task preparation belong to later phases.

`remote_data` defaults to `none`, which prevents a Gemini request. A trusted
operator may explicitly configure `prepared-repository` to share the prepared
objective, task patch, review report and check evidence. Review the allowed paths
and material before enabling it; this is not permission for unrestricted remote
repository access. `synthetic-fixture` is used only for generated evaluation
fixtures. The worker receives no Gemini credential.

A directory owns one immutable goal. Repeating `create` with the same intake is
idempotent and cannot refresh its deadline or budget. Restart `run` against that
same directory to resume. All spending, reservations, decisions, plans, worker
attempts and evidence persist. `status` can read progress while a runner owns the
service. Cancellation requires service ownership and reconciles interrupted
work; active-run signalling and pause controls are not implemented in this phase.

A delivered result has `phase=delivered`, a verified candidate identity, and a
worker task at `patch_ready`. Inspect its candidate under
`<directory>/worker/coding/<task-id>` and its linked run/acceptance evidence.
The original repository is unchanged. Existing explicit local acceptance remains
a separate operator action; delivery never implies commit, push, merge or deploy.

For the bounded live synthetic qualification:

```sh
.venv/bin/python scripts/qualify_supervisor.py --live
.venv/bin/python scripts/qualify_supervisor.py --live --clean
```

These use two fixed durable ledgers and $0.50 ceilings, each. They cannot reset
completed goals or reset spending on rerun. Both use the installed local Qwen
worker and a prepared Python container, and automatically rebuild the service
between coding and fresh review. They never send production repository content.
They are explicit paid evaluations, separate from network-free unit tests.

See [ADR-0018](../decisions/ADR-0018-durable-prepared-goal-supervisor.md) for
recovery, pricing, authority, budget and qualification limits.
