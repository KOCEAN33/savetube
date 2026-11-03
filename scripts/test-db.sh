#!/usr/bin/env bash
# test-db.sh <command...>: run a command from the repo root with
# SAVETUBE_TEST_DATABASE_URL pointing at the dev DB. Prints no secret.
set -euo pipefail
cd "$(dirname "$0")/.."
OP_SERVICE_ACCOUNT_TOKEN=$(security find-generic-password -s proxmox-talos-lab-op -w)
export OP_SERVICE_ACCOUNT_TOKEN
pw=$(op read 'op://Homelab/savetube-dev-db/password')
unset OP_SERVICE_ACCOUNT_TOKEN
pw=$(python3 -I -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$pw")
export SAVETUBE_TEST_DATABASE_URL="postgres://savetube:${pw}@pg-dev.lab.hiriri.com:5432/savetube?sslmode=verify-full&sslrootcert=$HOME/.postgresql/postgres-dev-ca.crt"
unset pw
exec "$@"
