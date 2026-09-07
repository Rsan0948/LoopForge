# Honest limitations

What LoopForge v1.0 deliberately does **not** do, and the accepted
boundaries a reader should not mistake for guarantees. Each entry names
the boundary, why it exists, and what it would take to lift it. House
rule: limits are documented, not implied (remaining-pacs-plan v1.0
acceptance: "documentation and release artifacts describe limits
honestly").

## Threat model and stores

- **Trusted-local operator stores (D10).** The eval-report and policy
  registry stores defend *consistency* — exact-key envelopes, schema
  versions, domain revalidation on every load, atomic tmp+rename writes —
  not *provenance*. With write access to the store directory an attacker
  can forge worse than a record; evidence-basis references are
  operator-resolved, never cryptographically bound. Lifting this needs
  signed artifacts and a trust root; out of scope for v1.0.
- **Single-operator-writer concurrency.** Per-instance locks and
  pid+uuid-namespaced tmp files defend the tmp path against threads and
  processes, and rename is atomic — but a cross-process
  read→transition→save is not a compare-and-swap. Two operators
  transitioning the same record concurrently get a last-writer-wins lost
  update. Lifting this needs flock/CAS on the store; accepted for a
  single-operator tool.
- **Store errors fail closed, not self-heal.** A corrupt, drifted,
  misnamed, or tampered artifact raises; nothing is repaired, skipped, or
  auto-migrated beyond the versioned v1→v2 shim. Operators restore from
  their own backups.

## Live models

- **Live outcomes are nondeterministic.** Live validation asserts
  structural invariants (terminal streams, grader coverage, zero false
  successes), never success rates. A live finding under
  `devstral-small-2:latest` is a configuration finding under that model,
  not a cross-model law.
- **The model sees only what the context assembly gives it — and that is
  the whole guarantee.** Prompt-injection resistance is containment
  (authority stays code-owned, so injected content cannot escalate), not
  immunity; a model can still be talked into bad *in-bounds* work.
- **Provider honesty is assumed for usage metering.** Token counts come
  from the provider response; budgets debit what the provider reports.

## Adaptive policies (PACS-017)

- **The policy vocabulary bounds adaptation by construction, not by
  audit.** Immutable surfaces (permissions, security boundaries, legal
  transitions, hard budgets, HITL, secrets, authority expansion) are
  unrepresentable in `ExecutionPolicy`; the guarantee is exactly as strong
  as that vocabulary's closed field set. A new knob is a reviewed code
  change, never data.
- **Shadow agreement is not improvement evidence.** A shadowed candidate
  that would have made the active decision proves nothing about the turns
  where it would differ; only locked-suite benchmarking speaks to that.
- **Console shadow pairing is a heuristic.** The session view pairs a
  shadow decision with the *nearest* enacted event of the matching kind —
  labeled "nearest enacted (heuristic)" in the UI — not a causal join.
- **Derived knobs can exceed a composition root's envelope.** Heuristic
  suggestions are clamped to the code-owned derivation envelope, but a
  composition root wiring a narrower envelope fails closed on widening;
  the suggestion is then inert, not adapted.
- **Shadow budget samples are pooled across policy ids** when read back
  from an event store; they shape a statistical suggestion, never an
  authority decision.
- **Counterfactual replay is deterministic re-drive only.** It re-executes
  from a stream prefix against recorded turns; it cannot simulate what a
  *different* model would have generated, and reports `incomplete` rather
  than guess.

## Benchmark laboratory

- **The suite is 12 categories, not the world.** Zero false successes is a
  harness-integrity invariant (verifier/grader divergence fails loudly),
  not a completeness claim about verification.
- **Graders re-derive from durable evidence only.** Anything the run did
  not journal cannot be graded; the event catalog is the audit boundary.

## Console and serving plane

- **The console is an operator tool on a trusted local network.** The
  serving plane has no authentication; it reads operator-owned stores and
  exposes one confirm-gated write (policy promotion). Do not expose it.
- **UI evidence links resolve by name, not by hash.** A basis naming a
  stored report links to it; the console does not verify the report's
  content is what the basis author saw.

## Engineering boundaries

- **Platform-gated tests skip with reason codes.** Suites that need
  `setrlimit(RLIMIT_AS)`, git, or Docker skip where those are unavailable
  (notably the local-sandbox launcher on this development platform); the
  skipped paths run in CI environments that provide the capability.
- **Local-only repository.** The project ships as a reference
  implementation; there is no hosted service, and release evidence is
  generated locally per the build map.
