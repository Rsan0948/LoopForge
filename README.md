# LoopForge

[![ci](https://github.com/Rsan0948/LoopForge/actions/workflows/ci.yml/badge.svg)](https://github.com/Rsan0948/LoopForge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/loopforge-console)](https://pypi.org/project/loopforge-console/)
[![Python](https://img.shields.io/pypi/pyversions/loopforge-console)](https://pypi.org/project/loopforge-console/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

LoopForge is a reference implementation and experimentation platform for **bounded autonomous software-engineering agents**.

Its core thesis is simple:

> A probabilistic model should operate inside a deterministic runtime that owns authority, state, verification, budgets, retries, and termination.

The coding-agent workload is a reference domain, not the product thesis. LoopForge exists to make agent execution **typed, replayable, observable, fault-tolerant, and scientifically evaluable**.

## Design principles

1. Models propose; the runtime enforces.
2. Environmental feedback beats prediction.
3. State is not conversation history.
4. Verification is independent of generation.
5. Every loop has finite resources.
6. High-risk side effects require explicit authority.
7. Adaptive policies can optimize within authority; they cannot expand authority.
8. Measure cost per completed task, not cost per inference.
9. Agent behavior should be explainable from state → action → outcome telemetry, not hidden reasoning.
10. Architecture is enforced by code and CI, not only documented.

## What LoopForge provides today

v1.0 is closed (PACS-001 through PACS-017). The invariant-bearing core was built first — a live model is one adapter among many, never the foundation of correctness:

- immutable domain events
- deterministic state projection
- explicit legal state transitions
- typed tool/model/state-store ports
- deterministic control policy
- budget and iteration governance
- code-owned tool authority metadata (risk, retry, idempotency, approval, timeout, sensitivity)
- model action proposals that cannot self-declare/downgrade authority
- an in-memory event store
- a durable SQLite append-only event store with optimistic concurrency
- safe replay/resume from persisted checkpoints
- event-backed action journal with durable attempt/idempotency semantics
- retry classification + bounded exponential backoff with deterministic jitter
- keyed/natural idempotent recovery from ambiguous tool outcomes
- per-run tool circuit breakers and deterministic no-progress detection
- USD/token/time/iteration budgets plus explicit cancellation
- a versioned JSON event serialization boundary
- a scripted model adapter for deterministic tests
- live model adapters for Ollama and DeepSeek behind the same typed `ModelPort`,
  with a capability registry and deterministic routing
- architecture contracts
- unit/property-style invariant tests
- deterministic fault-injection laboratory
- explicit trust/sandbox capability vocabulary
- constrained local sandbox reference adapter with fixed commands, filtered environment,
  file-API path/symlink defense, process timeout, resource limits, and bounded output
- a hardened Docker container sandbox adapter for untrusted repositories
- typed, provenance-aware model context artifacts (`ContextItem`/`ModelContext`) with
  code-owned trust authority, guarded elevation, and a durable `ContextAssembled` record;
  the model boundary consumes `ModelContext`, never raw run state
- an operator command center: durable sessions over REST + WebSocket with a React
  console, approval gates, follow-ups, and artifact rollback
- an execution provenance graph — a derived DAG over the event stream with inline
  explain chains for every side effect
- a locked 12-category benchmark and multi-trial evaluation laboratory with
  deterministic graders, trajectory metrics, and Pareto-frontier reports
- adaptive execution policies evaluated against that evidence in shadow mode;
  policies can optimize routing/context within authority, never expand it

The constrained local adapter explicitly does **not** claim child-process filesystem, network, or kernel isolation; untrusted repository execution goes through the container adapter. See `docs/security/threat-model.md` and `docs/architecture/sandbox-contract.md` for the full contract.

## Architecture

```text
entrypoints
    ↓
application
    ↓
ports
    ↓
domain

adapters ──implement──> ports
```

The domain package cannot depend on provider SDKs, databases, Git, HTTP, CLI frameworks, or telemetry vendors.

See `docs/architecture/overview.md` and the ADRs for rationale. The full product capability/dependency map lives in `docs/product/master-build-map.md`; development PACS cycles are explicitly manual and documented in `docs/process/manual-pacs.md`.

## Install (no clone needed)

Requires Python 3.12+. With [pipx](https://pipx.pypa.io):

```bash
pipx install loopforge-console
loopforge console
```

`loopforge console` serves the prebuilt console UI that ships inside the
package — no npm, no Postgres, no flags. It picks a free loopback port, stores
state under `~/.loopforge/console` (SQLite), and opens your browser.

One-line installer (checks Python, installs pipx if missing, then the package):

```bash
curl -fsSL https://raw.githubusercontent.com/Rsan0948/LoopForge/main/install.sh | bash
```

## Quick start (development)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
uv run pytest
uv run loopforge demo
```

The full quality gate set (format, lint, strict types, import contracts,
coverage) is listed in [CONTRIBUTING.md](CONTRIBUTING.md) and enforced by CI.

## Operator console

A local, trusted-operator console (REST + WebSocket API and a React SPA) drives
runs as durable sessions with an approval gate for operator-selected tools.

| Sessions | Session detail |
| --- | --- |
| ![Sessions view — run list and new-session form](docs/assets/console-sessions.png) | ![Session detail — live event stream, controls, artifacts](docs/assets/console-session-detail.png) |

| Benchmark evals | Policy registry |
| --- | --- |
| ![Benchmark suite and eval reports](docs/assets/console-evals.png) | ![Policy registry with evidence basis](docs/assets/console-policies.png) |

Postgres is the default session store and runs in Docker:

```bash
docker compose up -d loopforge-db   # postgres:17 on 127.0.0.1:5432 (loopforge/loopforge)
cd ui && npm ci && npm run build && cd ..
.venv/bin/python -m loopforge.entrypoints.cli serve \
  --dsn postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge \
  --static-dir ui/dist
# open http://127.0.0.1:8123
```

The server binds 127.0.0.1 only and has no authentication — it is a
trusted-operator local tool. `--sqlite PATH` swaps the store for a local
SQLite file with identical semantics. When `--static-dir` is omitted, `serve`
mounts the packaged console assets (or a checkout's `ui/dist`); from a
pipx/PyPI install, `loopforge console` is the zero-flags equivalent.
See `ui/README.md` and
`docs/process/cycles/PACS-014-operator-command-center.md`.

The Postgres integration suite never touches the server's database: it runs
against a dedicated `loopforge_test` database (created on demand, or override
with `LOOPFORGE_TEST_POSTGRES_DSN`) and fails closed on any DSN whose database
name does not end in `_test`.

## Operator usability notes

- **macOS**: the local sandbox cannot enforce the memory rlimit there (the
  platform rejects `RLIMIT_AS`), so the launcher skips only that limit and
  runs checks anyway; the skip is recorded honestly in the verification
  detail ("resource limits not enforced by this platform: RLIMIT_AS"). CPU,
  file-size, open-file, and wall-clock limits still apply. Use container mode
  for untrusted repositories: set `sandbox.container_image` in the inline
  form (or `[sandbox] container_image` in a TOML profile) and write check
  argv with in-container paths (`{python}` is not substituted in container
  mode).
- **Stall threshold**: runs stop after 3 consecutive verifications without
  score progress (`STOP_STALLED_NO_PROGRESS`). Small local models that read
  files for several turns before their first edit need headroom — set
  `[budget] no_progress_limit` in a profile or `budget.no_progress_limit`
  in the inline form (default stays 3).
- **Follow-up**: on a terminal (finished) run, "follow up →" creates a
  quiescent successor session on the same repository whose objective opens
  with the original task and closes with a bounded, deterministic report
  consolidated from the finished run's durable events. The operator reviews
  the seeded objective and presses start — nothing auto-chains.

See `docs/process/cycles/PACS-014b-operator-usability.md`.

## Documentation

- `docs/architecture/overview.md` — system overview; `docs/architecture/` also
  covers the kernel contract, durable event store, sandbox contract,
  reliability control plane, benchmark methodology, and honest limitations
- `docs/decisions/` — architecture decision records (ADR-0001 through ADR-0014)
- `docs/security/threat-model.md` — trust classes, hostile-content assumptions,
  and what the sandbox does and does not guarantee
- `docs/product/master-build-map.md` — full capability/dependency map
- `docs/process/` — development process and per-cycle records (PACS-001…017)
- `ui/README.md` — operator console SPA layout and development workflow

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
quality gates every change must pass, and [AGENTS.md](AGENTS.md) for the
architecture invariants that apply to human and AI contributors alike.

## Security

LoopForge executes model-proposed actions and treats model output,
repository-controlled content, and external evidence as potentially hostile.
Please report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE) — copyright 2026 Ruben Sanchez.
