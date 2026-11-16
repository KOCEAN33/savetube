#!/usr/bin/env bash
# One savetube instance for local checks: a fresh seeded database and the app
# served with login skipped (e2e/app.py), kept running until "down".
# Usage: e2e/instance.sh up|doctor|down <dir>
#   up      build the frontend, seed a database, serve on a free port. Returns
#           once the API answers from the database.
#   doctor  read-only check that the instance in <dir> is the one "up" started
#           and still serves the checkout it was built from.
#   down    stop the server, drop the database, remove <dir>/work.
# up and down need SAVETUBE_TEST_DATABASE_URL, an admin URL whose role has
# CREATEDB (scripts/test-db.sh sets it). <dir>/instance.env holds db, port,
# base, pid and build. Nothing else in <dir> is touched.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -eq 2 ] || { sed -n '2,12p' "$0" >&2; exit 2; }
cmd="$1"
mkdir -p "$2"
dir="$(cd "$2" && pwd)"
work="$dir/work"
state="$dir/instance.env"

# The commit plus every uncommitted change under the served trees.
build_id() {
  {
    git -C "$root" rev-parse HEAD
    git -C "$root" diff HEAD -- backend frontend e2e
    git -C "$root" ls-files -o --exclude-standard -- backend frontend e2e
  } | shasum | cut -c1-12
}

need_db_url() {
  [ -n "${SAVETUBE_TEST_DATABASE_URL:-}" ] || { echo "SAVETUBE_TEST_DATABASE_URL is unset" >&2; exit 1; }
}

load() {
  db="" port="" base="" pid="" build=""
  # shellcheck disable=SC1090
  [ -f "$state" ] && . "$state"
  return 0
}

# True when pid is still the server this script started, not a reused pid.
ours() {
  [ -n "$pid" ] && ps -o command= -p "$pid" 2>/dev/null | grep -qF "uvicorn app:app --app-dir $root/e2e"
}

down() {
  load
  status=0
  if ours; then
    kill "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 15))
    while kill -0 "$pid" 2>/dev/null; do
      [ "$SECONDS" -lt "$deadline" ] || { echo "server $pid did not stop within 15 s" >&2; status=1; break; }
      sleep 0.2
    done
  fi
  if [ -n "$db" ]; then
    (cd "$root" && uv run --project backend python e2e/seed.py drop "$db") || status=1
  fi
  rm -rf "$work" "$state"
  return "$status"
}

up() {
  need_db_url
  [ ! -e "$state" ] || { echo "$state exists: run doctor or down first" >&2; exit 1; }
  mkdir -p "$work"
  trap 'status=$?; [ "$status" = 0 ] || { [ -f "$work/server.log" ] && cat "$work/server.log" >&2; down || true; }; exit "$status"' EXIT
  trap 'exit 130' INT TERM

  echo "== build frontend"
  build="$(build_id)"
  (cd "$root/frontend" && pnpm install --frozen-lockfile --silent \
    && pnpm exec tsc -b && pnpm exec vite build --outDir "$work/dist" --emptyOutDir --logLevel warn)

  db="savetube_test_$(date +%s)_$(openssl rand -hex 6)"
  echo "== seed $db"
  echo "db=$db" >"$state"
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
    exec nohup uv run --project "$root/backend" uvicorn app:app --app-dir "$root/e2e" \
      --host 127.0.0.1 --port "$port" --log-level warning
  ) >"$work/server.log" 2>&1 </dev/null &
  pid=$!
  printf 'db=%s\nport=%s\nbase=%s\npid=%s\nbuild=%s\n' "$db" "$port" "$base" "$pid" "$build" >"$state"

  # /healthz answers before the database does, so readiness is an API read.
  deadline=$((SECONDS + 60))
  until curl -fsS "$base/api/stats/summary" >/dev/null 2>&1; do
    if ! kill -0 "$pid" 2>/dev/null || [ "$SECONDS" -ge "$deadline" ]; then
      echo "server did not answer /api/stats/summary within 60 s" >&2
      exit 1
    fi
    sleep 0.5
  done
  echo "== ready $base (state: $state)"
}

doctor() {
  load
  failed=0
  check() {
    if "${@:2}" >/dev/null 2>&1; then echo "ok    $1"; else echo "FAIL  $1"; failed=1; fi
  }
  listens() {
    for l in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t); do
      [ "$l" = "$pid" ] || [ "$(ps -o ppid= -p "$l" | tr -d ' ')" = "$pid" ] && return 0
    done
    return 1
  }
  check "state file $state" test -n "$pid"
  check "server process $pid is the one up started" ours
  check "port $port is held by that process" listens
  check "$base/healthz answers" curl -fsS "$base/healthz"
  check "login is skipped (/api/me is the e2e user)" sh -c "curl -fsS '$base/api/me' | grep -q '\"sub\":\"e2e\"'"
  check "database $db answers (/api/stats/summary)" curl -fsS "$base/api/stats/summary"
  check "build $build matches the checkout" test "$build" = "$(build_id)"
  return "$failed"
}

case "$cmd" in
  up) up ;;
  doctor) doctor ;;
  down) need_db_url; down ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
