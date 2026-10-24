# Testing and checks

## DB tests

```sh
bash /tmp/savetube-testdb/run-tests.sh <repo root> make check
```

Pytest args can replace `make check`. The script reads `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` and the password from `op read 'op://Homelab/savetube-dev-db/password'`. It prints no secret. Parallel runs are safe.

Tests that need ffmpeg use Homebrew `ffmpeg` and `ffprobe` on this Mac. Without them on PATH they skip.

DB tests live in `backend/tests/test_db_*.py`, with shared fixtures and helpers in `conftest.py`.

## Refactor guards

- `backend/tests/test_e2e.py` runs uvicorn and the real `savetube worker` with `yt_dlp.YoutubeDL` faked through a `sitecustomize.py` on `PYTHONPATH`. It fails when an `/api` route has no call.
- `make e2e-smoke`, run through `run-tests.sh`, drives Playwright over 7 pages. The ids, aria-labels, roles and URL params it targets must stay.

## Local UI

- Playwright: `uvx --with playwright python -I <script>` with `channel="chrome"`.
- Frontend: pnpm (`pnpm run typecheck`, `build`, `lint`).
- `/tmp/savetube-library/serve.sh` serves a worktree build against `savetube_proto`, login skipped. `/tmp/savetube-playback/serve.sh` and `/tmp/savetube-subs/serve.sh` add an archive root.

## yt-dlp repros

The local image `savetube:after` holds the worker's yt-dlp and ffmpeg.

```sh
docker run --rm --entrypoint sh -v <empty dir>:/out savetube:after -c "cd /out && yt-dlp ..."
```

## Production reads

```sh
kubectl --context homelab-tailnet -n postgres exec postgres-2 -c postgres -- psql -d savetube -c "set default_transaction_read_only=on;" -c "<one query>"
```

`kubectl exec deploy/savetube-worker -c worker -- <cmd>` runs commands such as `find /archive` and `savetube extract-audio`.

`psql` 18 comes from Homebrew `libpq`.
