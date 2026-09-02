# ADR-0010 — Serving-plane dependency relaxation for the operator command center

Status: Accepted

## Context

Before PACS-014 the runtime dependency posture was deliberately minimal: `httpx`
(PACS-011, first HTTP client) and `pydantic` only, with the domain/ports/application
packages dependency-free and every vendor quarantined behind a port adapter. The
operator command center introduces two new infrastructure needs that no existing
adapter covers:

1. a **serving plane** — REST + WebSocket API and static SPA hosting for the
   operator console (D2/D8/D10: trusted-operator local tool, loopback bind, no auth);
2. a **shared durable store** — multiple server processes and CLI clients must
   attach to the same event streams, which the single-writer SQLite store cannot
   provide (D3).

## Decision

Relax the dependency posture for exactly three runtime dependencies, each
quarantined to one architectural layer, plus one dev-only dependency:

- **`fastapi`** — the ASGI framework for the operator server. Used ONLY in
  `entrypoints/server.py` (and its tests). Routes are thin projection/command
  shims over `SessionManager`; no FastAPI type crosses into `application/`,
  `ports/`, or `domain/` (enforced by the existing import-linter layering and
  by code review: pydantic request models never leave the entrypoint).
- **`uvicorn[standard]`** — the ASGI server, invoked only by the `serve` CLI
  command (`entrypoints/cli.py`, lazy import). The `[standard]` extra is
  required: without `websockets` a real server process cannot answer WS
  handshakes at all (the in-process TestClient masked this until the first
  live browser round-trip).
- **`psycopg[binary,pool]`** — the Postgres driver, used ONLY by
  `adapters/postgres_events.py`. The adapter mirrors the SQLite store's
  semantics exactly (append-only triggers, compare-and-append,
  `StreamVersionConflictError`/`DuplicateEventError` parity, schema migrations
  with version rejection), pinned by a conformance suite ported from the
  SQLite tests and run against a real Postgres in Docker (`docker-compose.yml`,
  skip-gated when unavailable — rule 9: no network in unit tests).
- **`httpx2`** (dev extra only) — Starlette's `TestClient` is statically typed
  against `httpx2` via a `TYPE_CHECKING` import; without it pyright-strict sees
  every TestClient call as `Unknown`. At runtime Starlette falls back to the
  already-present `httpx`, so this is a type-check-time dependency only.

Event-schema versioning note: the JSON codec fails closed on unknown event
types, so a binary predating a catalog extension refuses to open streams
written by a newer one. Catalog growth therefore requires the operator to
upgrade all attached binaries (server and CLI) together; `SCHEMA_VERSION`
stays 1 for append-compatible additions of new event types.

## Consequences

Positive:

- the operator console ships without inventing an HTTP/WS stack inside the
  runtime's authority boundary; the server remains a projection + command
  issuer with every mutation flowing through the `Runtime` as durable events;
- the serving plane is swappable: `create_app` is a factory over
  `ServerSettings`, and tests drive the full surface in-process (no network);
- Postgres unlocks multi-process durability (server + CLI + restart
  rediscovery) with SQLite byte-for-byte semantics preserved for local use;
- deterministic CI is unchanged: PG conformance tests skip without a live
  database, and the default suite needs no Docker, no credentials, no network.

Negative:

- the supply chain grows by three runtime packages (plus transitive
  `websockets`, `anyio`, `starlette`, etc.) — all pinned in `uv.lock`;
- the domain's purity argument now rests on import-linter enforcement rather
  than on the absence of the packages from the environment;
- `uvicorn[standard]` pulls optional performance extras (`uvloop`, `httptools`,
  `watchfiles`) that are inert for a loopback operator tool but present in the
  environment.
