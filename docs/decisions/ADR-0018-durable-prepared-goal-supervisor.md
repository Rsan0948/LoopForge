# ADR-0018: Durable prepared goals and a bounded Gemini supervisor

Date: 2026-10-20  
Status: implemented; prepared-task qualification only

## Decision

A goal owns a sequence of bounded worker runs. Its append-only event journal is
separate from the existing campaign/run journals, whose reducers remain the
source of truth for worker attempts, verification and candidate disposition.
Application coordination depends only on supervisor, journal, clock and prepared
worker ports. Composition connects the existing local Qwen coding/review engine;
no generic campaign lane or external chat acknowledgement drives goal progress.

Phase 2 deliberately accepts an **operator-prepared task**: pinned repository
revision, allowed paths, immutable image, fixed commands, trusted acceptance
probes, deliverables and limits. Flexible investigation and model-proposed task
preparation remain Phase 3. The supervisor cannot author or modify that contract.
The entire intake, including worker configuration and remote-sharing scope, is
hashed into the immutable goal policy. Legacy campaign tasks are not migrated.

The supervisor chooses `delegate`, `verify`, `retry`, `deliver` or `block` through
an extra-fields-forbidden schema. Risk, permission, side effects, idempotency,
approval, sensitivity and timeout metadata are code-owned. Allowed actions derive
from actual worker state. Delivery additionally rechecks the candidate identity,
trusted acceptance receipt and successful independent verification run. Model
claims cannot establish verification or permit acceptance. Delivery retains a
`patch_ready` candidate; it does not commit, merge, push or deploy it.

## Crash recovery and idempotency

The service holds exclusive campaign-ledger ownership for the coordinator loop.
Journal writes use expected event sequence checks and `BEGIN IMMEDIATE`.

Before inference, a reservation event records a stable decision key, maximum
charge and model/settings/input-bound trace. A response atomically replaces the
reservation with its charge and validated proposal. If the process dies before
reconciliation, restart charges that reservation in full and records an unknown
outcome. It never resends that inference under the same reservation. A bounded
successor call may follow backoff if policy still permits it.

Before external worker actions, the composition journals their decision key,
original stage and attempt counters. Replays reconcile the existing task/run
rather than enqueue another task. Coding retries cannot accidentally dispatch
review under an old key. An interrupted run is cancelled through the existing
runtime event/reducer path; a successor is a new run. A campaign left in
attention resumes only as part of its recorded prepared successor intent. A
claimed dispatch that never started has one separately keyed recovery dispatch;
repeated admission failure blocks and preserves work.

Worker retry totals retain the existing immutable task token/time ceilings.
The goal deadline also narrows each new worker runtime's remaining allowance.
Provider failures have bounded total attempts and persisted exponential backoff.
Unknown or denied worker admission does not spin an unbounded paid loop.

## Gemini transport, context and budget

The adapter uses `generateContent` structured output for **self-contained
stateless decisions**. Each request includes the goal, prior durable plan,
current evidence and allowed actions. This is not a simulated multi-turn tool
conversation. Raw returned content parts (including opaque thought signatures),
usage and model version are retained in the local diagnostic trace; no provider
objects enter the core. Tool-call conversations are not exposed in Phase 2.

Only explicit `synthetic-fixture` or `prepared-repository` sharing enables a
remote request. The default `none` blocks before inference. The prepared bridge
exports task evidence, omitting host workspace paths and raw worker-provider
observations. Oversized evidence is explicitly identified as omitted; the
supervisor must block when that leaves insufficient evidence. Repository-wide
retrieval and generalized secret scanning are not implemented here: production
sharing requires deliberate configuration of the prepared material.

Credentials load deliberately from one named key or an explicit file, without
sourcing shell code or exporting a value into worker containers. The key travels
in an HTTP header and is excluded from prompt/trace material. Provider error
bodies and transport exceptions are replaced by safe diagnostics.

Requests are bounded to 48,000 UTF-8 bytes; input reservations use serialized
request bytes plus 4,096 protocol-framing tokens as a conservative upper bound.
This is an estimator, not a tokenizer measurement. Output is capped at 8,192
tokens including thinking, with medium thinking. HTTP timeout is 120 seconds;
there are no hidden transport retries, search tools, media or grounding charges.
Responses are limited to 1 MB. Invalid, truncated or inconsistent output never
becomes a proposal.

Budget values use integer micro-US dollars. Reservations count full uncached
input plus maximum output. Reported candidate and thinking tokens are both
charged; cached input is conservatively charged at the full input rate. Missing
usage and uncertain responses consume the whole reservation. Reservations and
settled spending across goals in the same journal share an immutable envelope.
Goal budgets never reset on worker retries or process restarts. Independent
installation directories have independent envelopes and must be allocated within
the operator's overall authorization; there is no account-wide billing service.

The code-owned price lock is $2/M input and $10/M output, including thinking,
verified against [Google pricing](https://ai.google.dev/gemini-api/docs/pricing)
on October 20. New calls fail closed on December 1 until prices are revalidated.
See also the [REST protocol](https://ai.google.dev/api/generate-content) and
[structured-output contract](https://ai.google.dev/gemini-api/docs/structured-output).

## Qualification and limits

Hermetic tests cover interrupted inference, unknown costs, shared reservations,
invalid decisions, authority denial, stale delivery, terminal states, retries,
immutable intake and exclusive service ownership. Real-container fixture tests
exercise intake through delivery, lost acknowledgement and restart, relative
paths and interruption between retry persistence and dispatch.

Paid tests send only a generated arithmetic fixture. The first run exposed
relative-path cloning and campaign-resume defects and required implementation
repair; this intervention is explicitly counted. A separate clean run completed
from intake through coding, automatic service restart, fresh review and candidate
delivery without chat intervention or implementation repair during that run.
Both retained original source bytes and left candidates unaccepted.

The two qualification ledgers each have a preallocated $0.50 ceiling, totaling
at most $1 inside the user's $5 authorization. Actual conservatively accounted
charges were $0.011454 for the repaired run and $0.009554 for the clean run:
**$0.021008 total**. These are token-usage estimates, not a verified account bill.
One clean synthetic task does not establish general autonomous reliability.

This phase does not add flexible task preparation, clarification timeouts,
experiments, a goal UI, live in-flight pause/cancel signalling, a production
scheduler or migration, notifications, automatic acceptance, or unattended
rollout. CLI cancellation owns the ledger, reconciles interrupted inference and
worker state, and preserves candidate work. A concurrently running coordinator
retains ownership; its active worker is not falsely reported as cancelled.
