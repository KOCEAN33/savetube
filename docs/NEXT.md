# Next

## Goal

Take the savetube scaffold to a deployable state on the homelab cluster, then follow `docs/plan.md` § Build order.

## Current progress

- This repository holds the whole scaffold, pushed to origin main.
  - Backend `src/savetube/`: config, OIDC login (`web/auth.py`), read API (`web/api.py`), schema plus alembic `migrations/versions/0001_initial_schema.py`, storage pipeline (`pipeline.py`, `jobs.py`), procrastinate queue (`queue.py`), recorder Job from the CronJob template (`k8s.py`), CLI (`cli.py`).
  - Frontend `frontend/`: Vite, React and shadcn `dashboard-01`, read-only pages.
  - Helm chart `charts/savetube/`. `tests/test_chart.py` holds 10 tests, written test-first from three anchors the user confirmed. `helm lint` passes and `kubectl apply --dry-run=server` accepts the render.
  - `compose.yaml` runs on OrbStack. The image builds, every role starts, and the worker downloads a public video. This was verified against a local PG 18 container, not pg-dev.
  - `make check`: 47 passed on pg-dev, PG 18.
- homelab-platform `c210755`, pushed: role `savetube` with CREATEDB, Database `savetube`, ExternalSecret `savetube-dev-db`, and pg_hba isolation on postgres-dev. Argo CD has applied them.
- 1Password `Homelab/savetube-dev-db` exists, with field `password`, 64 hex characters, so the DSN needs no escaping.
- Not built: live detection, source polling, write API, Discord notifications, job-state metrics, cancel, the `missed` reconciliation, ServiceMonitor, canary CronJob. `scheduler.py` samples storage only.

## What worked

- DB tests against pg-dev: export `SAVETUBE_TEST_DATABASE_URL` in a subshell, with the password from `op read 'op://Homelab/savetube-dev-db/password'`. Take `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` first.
- `SAVETUBE_ENV_FILE=<file> docker compose up` points the stack at another env file, without touching `.env`.
- Chart tests render with `helm template` and feed the rendered CronJob into the real `recorder_job()`.

## What did not work

- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`. `tests/test_auth.py` `FakeIssuer` patches `httpx2.AsyncHTTPTransport` instead.
- Without `relationship()`, SQLAlchemy does not order inserts by FK, so `tests/test_db.py` flushes parents first.

## Open questions

- Should yt-dlp get the `curl-cffi` extra? The worker logs "no impersonate target is available".
- Did a PO token reach a request? `bgutil:http-2.0.2` registered as available, but no log line shows a token being fetched.
- The image is 1.22 GB.
- The pot-provider sidecar runs without `readOnlyRootFilesystem`. Whether the image writes to its root filesystem is unchecked.
- The frontend bundle is 930 kB, and `frontend/README.md` is still the Vite template.
- The tests in `test_layout`, `test_ytdl`, `test_auth`, `test_k8s` and `test_db` were written beside the code, not test-first. A mutation pass would show what they miss.
- Zitadel project and application `savetube` are not registered. That is console work, per homelab-platform `docs/zitadel-oidc-setup.md`. Local login needs a Native application with redirect `http://127.0.0.1:8000/auth/callback`.

## Next steps

1. Create `homelab-workloads/savetube/appset.yaml` and `savetube/envs/prod.yaml`. The user approved this.
   - Copy the Helm shape in homelab-workloads `README.md`, plus `lrr-reader/appset.yaml`.
   - The Application is multi-source: the chart from `git@github.com:KOCEAN33/savetube.git`, path `charts/savetube`, and the values through a `$values` ref.
   - It needs the ExternalSecrets `savetube-db` (key `url`, password escaped with `urlquery`), `savetube-session` and `harbor-pull`.
   - Check that Argo CD holds credentials for the savetube repository.
   - Expect ImagePullBackOff until a Harbor image exists, and Pending until the `tank/youtube` PV exists.
2. The production role and Database in homelab-platform `manifest/postgres/` need the user's explicit permission before the commit (homelab-platform `CLAUDE.md`).
3. Continue with `docs/plan.md` § Build order.

## Suggested skills

`tdd` for new seams, `gh-cli` for the push and repo checks.

## Pointers

- `docs/plan.md`: design and Build order
- `charts/savetube/values.yaml`
- `.env.sample` and `Makefile`
- homelab-platform `docs/deploying-to-kubernetes.md`
