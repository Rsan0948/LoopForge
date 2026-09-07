# ADR-0014 — Adaptive execution policies: bounded adaptation with evidence-gated promotion

Status: Accepted

## Context

PACS-017 makes execution policy adaptive: context-budget allocation, model
routing knobs, verification cadence, and worker-count preferences may vary
per policy version, candidate policies may be derived from collected
evidence, and execution history may inform future runs. The thesis of the
project — *adaptive execution may optimize within authority, but it cannot
expand its authority* — stands or falls on where the boundary between
"adaptive" and "authority" is drawn, and on who moves a candidate across
it. Four pressures shaped the decision:

1. **Self-grant pressure.** Any knob that touches permissions, security
   boundaries, legal state transitions, hard budgets, HITL requirements,
   secret handling, or authority-expansion rules must be impossible for a
   policy to express — not merely disallowed at runtime (AGENTS.md rules
   11, 12). A runtime check is a bypass target; an unrepresentable value
   is not.
2. **Evaluation-honesty pressure.** A candidate must be evaluated by
   evidence it cannot influence: shadowed against live runs without
   touching the active path, and benchmarked by the locked suite
   (ADR-0012). Advice that silently alters the run it measures is
   self-assessment, not evidence.
3. **Promotion pressure.** Moving a candidate to PROMOTED grants it the
   authority to shape future runs. If any runtime path could perform that
   move — or perform it without a referenced evidence basis — the
   adaptation loop would be self-authorizing (rule 16).
4. **Suggestion pressure.** Statistical heuristics over eval/shadow
   evidence are useful but unprincipled if they can produce policies the
   domain would never have accepted. A heuristic must be a *suggestion
   generator*, never an authority.

## Decision

1. **The policy vocabulary makes immutable surfaces unrepresentable.**
   `domain/policies.py` `ExecutionPolicy` carries exactly the adaptive
   knobs — routing (`PolicyRoutingKnobs`), context allocation
   (`ContextAllocationBounds`), verification cadence, worker-count
   preference — and nothing else. There are deliberately no fields for
   permissions, security boundaries, legal transitions, hard budgets,
   HITL requirements, secret handling, or authority-expansion rules.
   Every knob is validated at construction (closed enums, finite ranges,
   bool-rejecting numeric checks), so a malformed policy cannot exist.
2. **Shadow evaluation never enacts.** `CandidateShadowAdvisor` advises
   at the three adaptive decision points (model route, context budget,
   verification cadence) through its own router over the same registry;
   its decisions are journaled as `ShadowDecisionRecorded` audit events
   (catalog 26) and never feed the active path. The advisor's only view
   of the run is a read-only accounting snapshot. The byte-identity pin
   proves the active path is identical with and without a shadow; shadow
   failure is honest absence, never a run failure.
3. **Promotion is operator-owned, evidence-gated, and terminal.**
   `PolicyRecord` requires a referenced evidence basis at registration;
   the closed lifecycle (CANDIDATE → SHADOWED/BENCHMARKED →
   PROMOTED/RETIRED, both terminal) makes CANDIDATE → PROMOTED illegal,
   and supersession is register-plus-promote of a new version, never
   mutation. The only write surfaces are the operator CLI (which
   confirm-gates promotion) and the REST promote route (literal
   `confirm: true`, StrictBool). No runtime path performs transitions —
   a candidate cannot self-promote because the transition vocabulary is
   not reachable from the run.
4. **Stores are operator-owned; the serving plane reads plus one gated
   write.** `PolicyRegistryStore` mirrors `EvalReportStore` ownership
   (ADR-0012): versioned per-record JSON artifacts, atomic tmp+rename
   writes, exact-key envelopes, domain revalidation on every load — a
   tampered registry fails loudly. The server exposes list/detail reads
   and the single confirm-gated promote route; there is no register,
   delete, or generic write surface.
5. **Heuristics are bounded suggestions constructed through the domain
   gate.** `derive_candidate_policy` reduces eval reports and shadow
   budget samples through deterministic, clamped heuristics (p90 context
   ceiling inside a code-owned envelope, recovery-weighted stall
   threshold), and the result is constructed through `ExecutionPolicy`
   validation — the domain is the final fail-closed gate. Suggestions
   register as CANDIDATE with their evidence basis and are reviewed like
   any hand-authored candidate; zero-filled schema-v1 rows are excluded
   from the recovery mean rather than silently diluting it.
6. **Honest boundaries are documented, not implied.** The registry is a
   trusted-local single-operator-writer tool: atomic rename prevents torn
   files, not cross-process lost updates. Revalidation proves consistency
   (shape, keys, domain invariants), not provenance; evidence-basis
   references are operator-resolved, never cryptographically bound.

## Consequences

Positive:

- Adaptation is provably confined: the adaptive surface is a closed,
  validated vocabulary, and the immutable surface cannot be named in it.
- Shadow and benchmark evidence is auditable through provenance and
  telemetry, and the active path is pinned byte-identical under shadowing.
- The promotion pipeline from the master build map (execution history →
  candidate → locked benchmark → shadow decisions → operator review →
  promote versioned policy) is now executable end-to-end through shipped
  surfaces (CLI `--derive`, `--transition`, `--promote`; console policy
  views).
- Live validation (M10) benchmarks two execution policies on the locked
  suite with a hard zero-false-success invariant and journals live shadow
  decisions through the container composition root.

Negative / accepted:

- The policy registry cannot defend against a writer with filesystem
  access (trusted-local threat model, D10); authenticity binding would
  need signatures, deliberately out of scope.
- Derived ceilings above the composition root's wired envelope are inert
  (the registry feeds no runtime directly; the envelope fails closed on
  widening), so heuristic suggestions may exceed what a given composition
  root will honor — accepted, surfaced in the rationale.
- Shadow budget samples are pooled across policy ids when read back from
  an event store; they are evidence shaping a suggestion, never authority.

## Alternatives considered

- **Runtime-enforced immutability (deny checks on a broad policy schema):**
  rejected — a schema that can name authority plus a check that denies it
  is a bypass target; an unrepresentable vocabulary is not (rule 11).
- **Auto-promotion on threshold metrics:** rejected — rule 16 requires an
  explicit, referenced evidence basis and an operator act; threshold
  promotion is self-authorization with extra steps.
- **Shadow-as-preview (enact shadow decisions when they agree):**
  rejected — agreement-enactment makes the measured run depend on the
  candidate, contaminating the evidence and violating the byte-identity
  guarantee that makes shadowing safe to leave on.
