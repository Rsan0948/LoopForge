#!/usr/bin/env bash
# LoopForge operator console — one-command launcher.
#
#   bash console.sh              build the UI if needed, serve the console
#   bash console.sh --build      force a UI rebuild first
#   bash console.sh --port 9000  serve on a different port
#
# Uses the seeded demo data (output/live-pacs017/demo) when present so every
# page has content; otherwise falls back to a fresh local store under
# .loopforge/server. Ctrl-C stops the server.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PORT=8177
BUILD=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --build) BUILD=1; shift ;;
    --port) PORT="${2:?--port needs a value}"; shift 2 ;;
    -h|--help)
      sed -n '2,11p' "$0"
      exit 0
      ;;
    *)
      echo "console.sh: unknown argument: $1 (try --help)" >&2
      exit 2
      ;;
  esac
done

# The console is a static build mounted by the backend; build it when missing.
if [[ ! -d ui/dist || $BUILD -eq 1 ]]; then
  echo "▸ building the console UI…"
  (cd ui && npm run build)
fi

DEMO=output/live-pacs017/demo
STORE_ARGS=()
if [[ -f "$DEMO/events.db" ]]; then
  echo "▸ seeded demo data found — serving it ($DEMO)"
  STORE_ARGS=(
    --sqlite "$DEMO/events.db"
    --evals-dir "$DEMO/evals"
    --policies-dir "$DEMO/policies"
    --data-dir "$DEMO/data"
  )
else
  echo "▸ no demo data found — using a fresh local store (.loopforge/server)"
  STORE_ARGS=(--sqlite .loopforge/server/events.db)
fi

echo "▸ console ready at: http://127.0.0.1:${PORT}  (Ctrl-C to stop)"
exec uv run loopforge serve \
  --host 127.0.0.1 \
  --port "$PORT" \
  --static-dir ui/dist \
  "${STORE_ARGS[@]}"
