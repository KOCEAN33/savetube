#!/usr/bin/env bash
# Frontend smoke: start a seeded instance (e2e/instance.sh), run e2e/smoke.py in
# Chrome, then stop the instance. Exits non-zero on any failure.
# Needs SAVETUBE_TEST_DATABASE_URL, an admin URL whose role has CREATEDB.
# Usage: e2e/run.sh   (or: make e2e-smoke)
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

dir="$(mktemp -d "${TMPDIR:-/tmp}/savetube-e2e.XXXXXX")"
cleanup() {
  status=$?
  "$root/e2e/instance.sh" down "$dir" || status=1
  rm -rf "$dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

"$root/e2e/instance.sh" up "$dir"
# shellcheck disable=SC1091
. "$dir/instance.env"

echo "== smoke"
# shellcheck disable=SC2154  # base comes from instance.env
uvx --with playwright python -I "$root/e2e/smoke.py" "$base" "$dir/work/seed.json" \
  ${SAVETUBE_E2E_SHOTS:+"$SAVETUBE_E2E_SHOTS"} || {
  echo "-- server log" >&2
  cat "$dir/work/server.log" >&2
  exit 1
}
