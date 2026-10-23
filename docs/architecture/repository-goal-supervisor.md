# Repository goal supervisor

Phases 3 and 4 add investigation, clarification experiments and local review to the durable goal loop.
It uses its own opt-in ledger and leaves production campaign state untouched.

```sh
loopforge-repository-supervisor create --config repository-goal.json --directory .loopforge/my-goal
loopforge-repository-supervisor run --directory .loopforge/my-goal --key-file .loopforge/secrets.env
loopforge-repository-supervisor status --directory .loopforge/my-goal
loopforge-repository-supervisor cancel --directory .loopforge/my-goal
```

Without reinstalling console scripts, use
`.venv/bin/python -m loopforge.entrypoints.repository_supervisor` with the same
arguments. `status`, `report`, answers and control requests can run while the
runner owns the service. In-flight work is reconciled before pause/cancel is
acknowledged.

The strict `RepositoryGoalConfig` builds on the prepared-goal schema:

- `campaign` supplies the repository, explicit commit, read scope and local worker
  settings. Working-tree changes enter the checkpoint only when explicitly captured.
- `task` supplies the original objective, editable prefixes, branch, trusted
  acceptance probes and aggregate worker limits. Leave `required_changed_paths`
  empty: the supervisor discovers those files.
- `environments` lists approved IDs, descriptions, immutable image digests, fixed
  readiness commands and fixed project checks. Images must already be installed.
- `max_cost_microusd`, goal duration, decision/failure limits and meaningful-progress
  limits apply across investigation, worker attempts, review and restart.
- `remote_data` defaults to `none`. Production sharing requires deliberate
  `prepared-repository` configuration for the allowed source, objective, patch
  and measurement evidence. Evaluation uses `synthetic-fixture` only.

The supervisor can inspect a scoped directory or source page, prepare an approved
environment, reproduce independent behavior, retrieve original evidence by ID,
and propose an assignment with narrower files and evidence dependencies. It can
revise this approach using measurements rather than mandatory campaign lanes.
Its proposal cannot supply commands, image names, environment variables, probe
bodies or permissions. Assignment and recovery guidance remain model inference
in local worker context.

The evidence catalog exposes exact content identities and omissions. Original
records remain in `goals.sqlite` alongside immutable evidence receipts, and
worker runs remain in `worker/runs.sqlite`. A delivered result identifies a
verified candidate under `worker/coding/<task-id>` with its independent receipt.
It remains unaccepted and does not modify the source repository.

A generated multi-file live qualification is available separately from tests:

```sh
.venv/bin/python scripts/qualify_repository_supervisor.py --live
.venv/bin/python scripts/qualify_repository_supervisor.py --live --clean
```

These are paid opt-in evaluations using two fixed $0.75 envelopes. They inject
one recorded transient container failure, require source discovery and baseline
reproduction, and rebuild the service before delegation. Only generated fixture
material is shared. A completed or blocked evaluation ledger cannot be reset by
rerunning the command.

See [ADR-0019](../decisions/ADR-0019-repository-investigation-and-task-preparation.md)
for controls, qualification evidence and scope limits, and the
[prepared-goal guide](prepared-goal-supervisor.md) for the underlying coordinator.

## Phase 4: clarification, experiments and local review

Trusted intake can add one clarification topic:

```json
{
  "capture_working_tree": true,
  "clarifications": [{
    "topic": "presentation",
    "question": "Which presentation should the summary use?",
    "classification": "optional",
    "timeout_seconds": 300,
    "fallback": "Preserve the existing public API and output shape"
  }]
}
```

Use `missing-information` or `permission` with an empty fallback for restricted
questions. Silence never grants permission. Separately authorized research can
continue; dependent coding waits. Optional expiry records an assumption and
experiment branch from the frozen checkpoint. The runner never edits or merges
into the source checkout. With working-tree capture enabled, the checkpoint
includes tracked and nonignored untracked working bytes, preserves deletions and
modes, and records ignored exclusions. Capture requires the selected revision to
be the source HEAD. Unsupported or changing snapshots fail closed.

```sh
loopforge-repository-supervisor pause --directory .loopforge/my-goal
loopforge-repository-supervisor resume --directory .loopforge/my-goal
loopforge-repository-supervisor answer --directory .loopforge/my-goal \
  --topic presentation --answer 'Preserve the existing public API and output shape'
loopforge-repository-supervisor report --directory .loopforge/my-goal
loopforge-repository-supervisor accept --directory .loopforge/my-goal
loopforge-repository-supervisor discard --directory .loopforge/my-goal
```

Pause/cancel are durable requests; they take effect after in-flight work reconciles.
The `run` process stays alive while paused and services resume/cancel requests.
If it is not running, start it to reconcile pending controls. Deadlines and budgets
are never reset. A conflicting late answer pauses future work and blocks acceptance;
retain/discard the result and create a new goal with the clarified direction.

Accept requires a delivered, unchanged, independently verified candidate and commits
only the isolated branch. Discard records the operator choice and preserves files.
Both are idempotent and require the runner to release service ownership after the
goal terminates. Goal status remains terminal after either disposition.

For the console, put goal directories below its configured root:

```sh
loopforge serve --sqlite .loopforge/server/events.db \
  --data-dir .loopforge/server --goals-dir .loopforge/goals --static-dir ui/dist
# Create goals at .loopforge/goals/<goal-id>, then open #/goals.
```

The goal view shows questions, answers, assumptions, cost/reservations, current
plan, candidate diff, drift warnings, controls and accepted commit. Reports are
available from `/api/goals/<goal-id>` or the CLI. No email/Slack notification is
configured. Run the synthetic, network-free container qualification once with:

```sh
.venv/bin/python -m scripts.qualify_goal_review
```

It preserves its ledger and morning report; an existing qualification is not reset.
See [ADR-0020](../decisions/ADR-0020-clarification-experiments-and-goal-review.md)
for limits and cross-phase hardening.
