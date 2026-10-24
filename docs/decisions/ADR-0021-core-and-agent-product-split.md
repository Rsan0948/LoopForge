# ADR-0021: Separate the open-source core from the agent product

Date: 2026-10-24  
Status: accepted (user decision). The physical split is deferred; this record governs where new
work goes from now on.

## Context

The repository holds two things (see `docs/process/product-definition-and-research-2026-10-24.md`):

- **The core (A):** the deterministic-runtime reference architecture. It is complete, open
  source, and published as `loopforge-console`.
- **The agent product (B):** the unattended repository agent. Gemini supervises, a local Qwen
  model codes, and the result is a verified, reviewed, unmerged candidate. This is the tool the
  user actually wants to use.

Since 2026-10-20, B has been built inside A's `entrypoints/`, with benchmark-specific code mixed
in. The architecture audit found that this erodes A's standards without giving B a clean home.

## Decision

1. **Two projects.** A stays the pure core. B becomes a separate application that **depends on
   A** and uses its source directly, as an ordinary Python dependency. There is no middleware, no
   service boundary and no plugin framework; B imports A's modules.
2. **One-way dependency.** A never imports B. B imports A's public packages: `domain`, `ports`,
   `application`, `workloads`, and the generic `adapters`.
3. **General improvements go into A.** Anything built for B that is general goes upstream into A,
   with A's standards: tests, AGENTS.md rules, CI. Examples: event-sourcing fixes, runtime and
   reliability fixes, generic adapters, gate-policy machinery, typed errors. B keeps only what is
   specific to the product.
4. **Positioning.** Open-source and reference-architecture concerns (public API stability,
   documentation, packaging, demos) apply to A only. B is the user's own tool and is optimized
   for use, not for presentation.
5. **B's scope.** Repair and feature work. From-scratch builds are no longer a goal, and
   build-only mechanisms are candidates for removal: construction credit, from-scratch review
   handling, and the Snake build trials.

## Boundary (initial assignment)

| Stays in A (core) | Moves to B (agent product) |
|---|---|
| `domain/` (events, run state, policy, goals reducer if kept generic, investigation vocabulary) | Repository-goal composition: `repository_supervisor`, `goal_launch`, `goal_runner`, `goal_worker`, `goal_investigation`, `goal_guidance`, `goal_review` and related modules |
| `ports/` | The coding workload as B uses it: `campaign_coding`, `campaign_execution`, `campaign_review` and the coding store, refactored per the architecture audit |
| `application/` (`Runtime`, orchestrator, eval runner, telemetry, `GoalCoordinator` if kept generic) | Gemini supervisor and reviewer prompts and wiring |
| `workloads/` (repair, benchmarks, fixtures) | The MLX engine (transport, `scripts/mlx_server_agentic.py`, renderer) and its service tooling |
| Generic adapters (event stores, sandboxes, `OllamaModel`, DeepSeek, git workspace, file tools, the Gemini HTTP core if generic) | Trial tooling: comparison and smoke runners, `prove_goal_gates`, `.loopforge/tasks` runners |
| Core console, server, CLI and eval/policy surfaces | The product's process docs from Oct 20 onward |
| AGENTS.md, ADR-0001…0017, core docs | ADR-0018…0021 copied over as product history |

Decided by the user on 2026-10-24, after the initial record:

- **The Brook/HeliCops review-campaign product is retired** for now: review lanes, rounds,
  focused per-project prompts, issues, its CLI and its FastAPI app. The coding coordinator the
  goal path drives is kept, because B needs it, until it moves to B.
- **Legacy inference profiles are retired.** Only `qwen-native-v1` and `qwen-bounded-v3` (the
  known-good control) remain. Repair escalation and repair decisions are retired with
  `qwen-state-v1`.
- **B is named WorkBuddy**, with a local repository at `~/Developer/WorkBuddy` and a GitHub
  repository `Rsan0948/WorkBuddy`. It depends on A through an editable `uv` path source.

Still open:

- **Modules claimed by both sides,** such as the goal reducer and `GoalCoordinator`: A keeps them
  only if they are generic goal control, with nothing about Gemini, campaigns or benchmarks.

## Consequences

- New B work is written so it can move: it imports A only through the public packages above, and
  is never added to A's `entrypoints/`.
- The architecture-audit steps are split. Steps 0 (hygiene, CI) and 3's generic gate machinery
  land in A. The composition root, configuration records and the workload move are B's
  structure.
- The bug plan (`full-audit-2026-10-24.md`) is reprioritized for repair and feature work. F15
  (from-scratch review cost) and construction credit drop. F2's probe partial credit and R1's
  feature ledger remain, because feature work has the same "progress inside a probe" problem.
- Until the split happens, both live in this repository. Import-linter can enforce the boundary
  early with an independence contract once B's modules move under one package.
