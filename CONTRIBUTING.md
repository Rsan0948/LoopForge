# Contributing

Contributions are welcome — bug reports, design discussion, documentation, and
code. By submitting a contribution you agree it is licensed under the project's
[Apache License 2.0](LICENSE).

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
uv run pytest
```

For console UI work also see `ui/README.md` (Node 22, `npm ci` in `ui/`).

## Quality gates

Every change must pass the same gates CI runs:

```bash
ruff format --check .
ruff check .
pyright
lint-imports
pytest --cov=loopforge --cov-branch
```

UI changes must additionally pass, in `ui/`:

```bash
npm run typecheck
npm run build
```

## Ground rules

- The architecture invariants in [AGENTS.md](AGENTS.md) apply to humans and AI
  coding agents alike — read them before touching `src/`. CI enforces the
  dependency DAG (`lint-imports`) and architecture contract tests.
- No network access in unit tests. Live-model tests live in `tests/live/`
  behind skip probes and never run in CI.
- A new control rule requires tests for both the allowed behavior and the
  denial/failure behavior.
- Model output crossing a system boundary must be validated by a strict schema;
  keep `Any` out of the core packages.
- Sandbox capability claims must be backed by the adapter's capability
  contract — never describe local adapters as strong isolation.

## Architecture changes

Architecture changes should include or update an ADR in `docs/decisions/`.
Large changes are best discussed in an issue first so the authority,
verification, and budget implications are explicit before code is written.
