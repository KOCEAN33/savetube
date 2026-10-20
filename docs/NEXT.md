# Next

## Goal

Follow `docs/plan.md` § Build order. savetube runs in production on the homelab cluster, but only its scaffold exists.

## Current progress

- The repository is public under MIT. A gitleaks pass over the full history found no secret before the switch.
- Branches: work lands on `develop`, the default branch. A push to main is a release.
- Build: `.github/workflows/image.yml` runs on a GitHub-hosted runner on every push to main. python-semantic-release (`releaserc.toml`) first tags the next `vX.Y.Z` and creates a GitHub Release. The image gets `X.Y.Z`, `X.Y` and the full commit sha, or only the sha when no commit bumps the version. The GHCR package is public, and the node pulls it through Harbor's `ghcr.io` pull-through cache with no pull secret.
- The app does not know its own version. The image carries it only as the OCI label `org.opencontainers.image.version`.
- Deploy: homelab-workloads `savetube/` holds the ApplicationSet, `envs/default.yaml`, `envs/prod.yaml`, and `manifests/prod/` with the ExternalSecrets `savetube-db` and `savetube-session` plus the static PV and PVC `savetube-archive`. Application and namespace are `savetube-prod`, and the release name is `savetube`.
- Running in `savetube-prod`: web, worker and scheduler. Migration `0001` applied. `https://savetube.lab.hiriri.com/healthz` answers 200, and `/auth/login` redirects to Zitadel with PKCE.
- A browser login has not been tried end to end.
- Zitadel: project `savetube`, Web application with PKCE (auth method `None`), grant type Authorization Code, client ID `395908165327192123` in `envs/prod.yaml`.
- Postgres: the production role and `Database` `savetube` on Cluster `postgres` (homelab-platform `manifest/postgres/`). 1Password items `savetube-db` and `savetube-session`, each one field (`password`, `secret`).
- Storage: `tank/youtube` (proxmox-talos-lab inventory), mounted rw at `/var/mnt/youtube`, root owned by 1000.
- Not built: live detection, source polling, write API, Discord notifications, job-state metrics, cancel, the `missed` reconciliation, ServiceMonitor, canary CronJob. `scheduler.py` samples storage only.

## What worked

- DB tests against pg-dev: export `SAVETUBE_TEST_DATABASE_URL` in a subshell, with the password from `op read 'op://Homelab/savetube-dev-db/password'`. Take `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` first.
- `SAVETUBE_ENV_FILE=<file> docker compose up` points the stack at another env file, without touching `.env`.
- Chart tests render with `helm template` and feed the rendered CronJob into the real `recorder_job()`.
- Off the home LAN, the Talos API answers only on the tailnet: `talosctl --talosconfig talos/generated/talosconfig -e talos-pve1.taila166d5.ts.net -n 192.168.33.20`. `make restore` refreshes that talosconfig. Ansible reaches pve1 with `ARGS='-e ansible_host=100.69.133.35'`.

## What did not work

- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`. `tests/test_auth.py` `FakeIssuer` patches `httpx2.AsyncHTTPTransport` instead.
- Without `relationship()`, SQLAlchemy does not order inserts by FK, so `tests/test_db.py` flushes parents first.
- `make talos-config` connects to the node's LAN address only, so it fails off the home LAN. A single new document goes in with `talosctl patch mc --mode no-reboot` over the tailnet, after a `--dry-run` that shows the diff.
- `make tank ARGS='--check'` fails at the SMART report: check mode skips `smartctl`, and `from_json` gets empty output.

## Open questions

- Should yt-dlp get the `curl-cffi` extra? The worker logs "no impersonate target is available".
- Did a PO token reach a request? `bgutil:http-2.0.2` registered as available, but no log line shows a token being fetched.
- The image is 1.22 GB.
- The pot-provider sidecar runs without `readOnlyRootFilesystem`. Whether the image writes to its root filesystem is unchecked.
- The frontend bundle is 930 kB, and `frontend/README.md` is still the Vite template.
- The tests in `test_layout`, `test_ytdl`, `test_auth`, `test_k8s` and `test_db` were written beside the code, not test-first. A mutation pass would show what they miss.
- `docs/plan.md` § Homelab deployment was meant to move to homelab-workloads before the repository went public. It holds hostnames and infrastructure layout, no secrets.
- No deploy job updates `envs/prod.yaml`. A new image needs a hand edit of `image.tag`. lrr-reader's deploy job is the pattern, and it needs the `kocean33-homelab-ci` App key as a savetube repository secret.

## Next steps

1. Log in through a browser at `https://savetube.lab.hiriri.com` as a user holding `savetube-admin`.
2. Continue with `docs/plan.md` § Build order.

## Suggested skills

`tdd` for new seams, `gh-cli` for workflow runs and repo checks.

## Pointers

- `docs/plan.md`: design and Build order
- `charts/savetube/values.yaml`
- homelab-workloads `savetube/`
- `.env.sample` and `Makefile`
- homelab-platform `docs/deploying-to-kubernetes.md`
