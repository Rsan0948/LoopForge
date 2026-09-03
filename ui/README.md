# LoopForge operator console (`ui/`)

Vite + React + TypeScript SPA for the PACS-014 operator command center. It is a
**projection + command issuer only**: every run-state mutation is a POST to the
operator server's REST API, and live updates arrive over the server's WebSocket
event stream (D6) — the console never writes run state directly.

## Layout

- `src/api.ts` — typed client for every REST endpoint + WS URL derivation
  (relative URLs; the SPA works under the backend's static mount with no config)
- `src/events.ts` — typed mirror of the `JsonEventCodec` wire envelope
  (25 event types, schema v1) + human-readable stream labels
- `src/views/SessionsView.tsx` — sessions list, new-session panel
  (profile picker or validated inline profile, incl. the approval gate)
- `src/views/SessionView.tsx` — session detail: WS-driven live event stream
  (sequence dedupe, reconnect backoff, REST reconciliation), approval banner,
  start/pause/resume/stop/instruct controls, objective amendment, artifacts,
  provenance panel (PACS-015: the derived DAG with inline explain chains —
  fetched on demand, refreshed with the stream only while open), lineage
  links (parent/children navigation), and a force-release control whose
  confirm is gated on an explicit "bypass replay checks" checkbox
- `src/DiffViewer.tsx` — `workspace_snapshot` artifact renderer with
  selective/full rollback controls
- Hash routing (`#/sessions`, `#/sessions/:runId`) so the static mount needs
  no SPA fallback rules

## Develop

```bash
cd ui
npm install
npm run dev        # Vite dev server; proxies /api and /ws to 127.0.0.1:8123
```

Run the backend alongside it (Postgres from the repo root first):

```bash
docker compose up -d loopforge-db
.venv/bin/python -m loopforge.entrypoints.cli serve \
  --dsn postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge
```

## Gates (run before committing UI changes)

```bash
npm run typecheck  # tsc --noEmit, strict — must be zero-error
npm run build      # tsc + vite build → ui/dist
```

## Serve the built console

```bash
.venv/bin/python -m loopforge.entrypoints.cli serve \
  --dsn postgresql://loopforge:loopforge@127.0.0.1:5432/loopforge \
  --static-dir ui/dist
# open http://127.0.0.1:8123
```

`ui/dist` and `ui/node_modules` are gitignored; rebuild after pulling UI
changes. The server binds 127.0.0.1 only (D10: trusted-operator local tool,
no authentication).
