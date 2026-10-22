#!/usr/bin/env bash
# Frontend smoke: build the frontend, seed a fresh database, serve the app with
# login skipped, run e2e/smoke.py in Chrome, then stop the server and drop the
# database. Exits non-zero on any failure.
# Needs SAVETUBE_TEST_DATABASE_URL, an admin URL whose role has CREATEDB.
# Usage: e2e/run.sh   (or: make e2e-smoke)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "${SAVETUBE_TEST_DATABASE_URL:-}" ] || { echo "SAVETUBE_TEST_DATABASE_URL is unset" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/savetube-e2e.XXXXXX")"
db="savetube_test_$(date +%s)_$(openssl rand -hex 6)"
server_pid=""
seeded=0
cleanup() {
  status=$?
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  if [ "$seeded" = 1 ]; then
    (cd "$root" && uv run --project backend python e2e/seed.py drop "$db") || status=1
  fi
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

echo "== build frontend"
(cd "$root/frontend" && pnpm install --frozen-lockfile --silent \
  && pnpm exec tsc -b && pnpm exec vite build --outDir "$work/dist" --emptyOutDir --logLevel warn)

echo "== seed $db"
seeded=1
(cd "$root" && uv run --project backend python e2e/seed.py create "$db" "$work/archive" "$work/seed.json")

port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
base="http://127.0.0.1:$port"
echo "== serve $base"
# The server runs in the work directory, so pydantic-settings finds no repo .env.
(
  cd "$work"
  export SAVETUBE_E2E_DB="$db"
  export SAVETUBE_STATIC_DIR="$work/dist"
  export SAVETUBE_ARCHIVE_ROOT="$work/archive"
  export SAVETUBE_SESSION_SECRET="e2e-only-not-a-secret"
  export SAVETUBE_OIDC_CLIENT_ID="e2e"
  export SAVETUBE_PUBLIC_URL="$base"
  exec uv run --project "$root/backend" uvicorn app:app --app-dir "$root/e2e" \
    --host 127.0.0.1 --port "$port" --log-level warning
) >"$work/server.log" 2>&1 &
server_pid=$!

deadline=$((SECONDS + 60))
until curl -fsS "$base/healthz" >/dev/null 2>&1; do
  if ! kill -0 "$server_pid" 2>/dev/null || [ "$SECONDS" -ge "$deadline" ]; then
    echo "server did not answer /healthz within 60 s" >&2
    cat "$work/server.log" >&2
    exit 1
  fi
  sleep 0.5
done

echo "== smoke"
uvx --with playwright python -I "$root/e2e/smoke.py" "$base" "$work/seed.json" || {
  echo "-- server log" >&2
  cat "$work/server.log" >&2
  exit 1
}
