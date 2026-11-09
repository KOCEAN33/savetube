# Testing and checks

## DB tests

```sh
make check-db
```

`make check-db` runs `scripts/test-db.sh make check`. For a subset, run `scripts/test-db.sh uv run --project backend pytest -q <args>`. The script reads `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` and the password from `op read 'op://Homelab/savetube-dev-db/password'`, with no user approval. It prints no secret. Parallel runs are safe.

Tests that need ffmpeg use Homebrew `ffmpeg` and `ffprobe` on this Mac. Without them on PATH they skip.

DB tests live in `backend/tests/test_db_*.py`, with shared fixtures and helpers in `conftest.py`.

## Refactor guards

- `backend/tests/test_e2e.py` runs uvicorn and the real `savetube worker` with `yt_dlp.YoutubeDL` faked through a `sitecustomize.py` on `PYTHONPATH`. It fails when an `/api` route has no call.
- `scripts/test-db.sh make e2e-smoke` drives Playwright over 8 pages. The ids, aria-labels, roles and URL params it targets must stay.
- `SAVETUBE_E2E_SHOTS=<dir>` makes the smoke save its screenshots there: the Downloads page filtered to failed jobs.

## Local UI

- Playwright: `uvx --with playwright python -I <script>` with `channel="chrome"`.
- Frontend: pnpm (`pnpm run typecheck`, `build`, `lint`).
- `e2e/app.py` serves the app with login skipped against a database `e2e/seed.py create` seeded. `e2e/run.sh` sets the env it needs, including the frontend build and archive root.

## yt-dlp repros

The local image `savetube:after` holds the worker's yt-dlp and ffmpeg.

```sh
docker run --rm --entrypoint sh -v <empty dir>:/out savetube:after -c "cd /out && yt-dlp ..."
```

## Production reads

The guard `postgres-admin-guard` refuses `kubectl exec` into the database pod. Ask the user to approve elevation, run `make -C ~/codes/kocean33/homelab/proxmox-talos-lab kube-elevate`, and read through the elevated kubeconfig until its `Valid until` time:

```sh
P=$(kubectl --context homelab-tailnet -n postgres get cluster postgres -o jsonpath='{.status.currentPrimary}')
KUBECONFIG=~/.kube/elevated/kubeconfig kubectl -n postgres exec -i "$P" -c postgres -- \
  env PGOPTIONS='-c default_transaction_read_only=on' psql -d savetube -X < queries.sql
```

To time an API query, print its SQL with `str(q.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))` and prefix it with `EXPLAIN (ANALYZE, BUFFERS)`.

`kubectl exec deploy/savetube-worker -c worker -- <cmd>` runs commands such as `find /archive`, `savetube extract-audio` and `savetube verify-recordings`.

`psql` 18 comes from Homebrew `libpq`.
