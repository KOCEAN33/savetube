# Next

## Goal

Check the archive features of `v0.2.0` in production through a browser, then review live recording with the user before building the recorder path.

## Current progress

- The repository is public under MIT. A gitleaks pass over the full history found no secret before the switch.
- Branches: work lands on `develop`, the default branch. main changes only through a merged pull request: the ruleset `main` allows no direct push, no force push, no bypass, and the merge-commit method only. A merge to main is a release and a deploy.
- Deploy: the `deploy` job in `image.yml` writes the new image tag into homelab-workloads `savetube/envs/prod.yaml` `image.tag` and pushes as the `kocean33-homelab-ci` App. Argo CD rolls it out. The job reads the variable `HOMELAB_APP_CLIENT_ID` and the secret `HOMELAB_APP_PRIVATE_KEY`, PKCS#8 from the 1Password item `kocean33-homelab-ci` in the Private vault. The `proxmox-talos-lab-op` service account cannot read that vault. The key field is labeled `개인 키`, so `op read` takes its field id `private_key`.
- actionlint reports `client-id` on `create-github-app-token@v3` as unknown. Its bundled metadata is stale, and v3 accepts the input.
- Build: `.github/workflows/image.yml` runs on a GitHub-hosted runner on every push to main. python-semantic-release (`releaserc.toml`) first tags the next `vX.Y.Z` and creates a GitHub Release. The image gets `X.Y.Z`, `X.Y` and the full commit sha. Every commit type bumps at least patch. Only a run with no new commit, such as a manual rerun, releases nothing and deploys the sha. The GHCR package is public, and the node pulls it through Harbor's `ghcr.io` pull-through cache with no pull secret.
- The app does not know its own version. The image carries it only as the OCI label `org.opencontainers.image.version`.
- Deploy: homelab-workloads `savetube/` holds the ApplicationSet, `envs/default.yaml`, `envs/prod.yaml`, and `manifests/prod/` with the ExternalSecrets `savetube-db` and `savetube-session` plus the static PV and PVC `savetube-archive`. Application and namespace are `savetube-prod`, and the release name is `savetube`.
- The chart is pinned: the ApplicationSet element's `chartRevision` names a savetube release tag, now `v0.2.0`. A release moves only `image.tag`. Raise `chartRevision` by hand when the chart changes, after the image the new chart needs is running. Before the pin, the chart tracked main: the `v0.2.0` merge synced the new livenessProbe onto the `0.1.2` image, and worker and scheduler restarted for about five minutes until the tag commit landed.
- savetube, homelab-workloads and homelab-platform each send push events to the Argo CD webhook `https://argocd.hiriri.com/api/webhook`. The secret is field `github-secret` of the 1Password item `argocd-webhook` in the Homelab vault.
- Running in `savetube-prod`: `v0.2.0` web, worker and scheduler. Migrations `0001`-`0004` applied. `https://savetube.lab.hiriri.com/healthz` answers 200, and `/auth/login` redirects to Zitadel with PKCE.
- A browser login has not been tried end to end.
- Zitadel: project `savetube`, Web application with PKCE (auth method `None`), grant type Authorization Code, client ID `395908165327192123` in `envs/prod.yaml`.
- Postgres: the production role and `Database` `savetube` on Cluster `postgres` (homelab-platform `manifest/postgres/`). 1Password items `savetube-db` and `savetube-session`, each one field (`password`, `secret`).
- Storage: `tank/youtube` (proxmox-talos-lab inventory), mounted rw at `/var/mnt/youtube`, root owned by 1000.
- Built and checked locally end to end: the Rules and Sources pages create and edit rules and sources, the scheduler polls due sources, the worker downloads into the tree. In production these are deployed and not yet exercised.
- Live detection runs in the scheduler and queues `record` jobs. A check against real channels found two live and two upcoming streams on NASA and none on VESPERBELL. Nothing records them yet.
- Not built: recorder Job creation, Discord notifications, job-state metrics, cancel, a manual retry, the `missed` reconciliation, ServiceMonitor, canary CronJob.
- Released in `v0.2.0` through pull request #4, checked locally against real VESPERBELL listings: channel playlists, title words at global, channel and source level, listing before downloading, download limits, stalled-job reclaim, `/healthz` with livenessProbe. The design is in `docs/plan/archive.md`.
- No CI runs on a pull request, because `image.yml` triggers only on a push to main.
- The user's decisions behind the design, which the plan states as rules: word lists merge by union with no per-source opt-out, a global include word restricts every source on purpose, words apply to live detection too, children of a "track all" source are disabled and never deleted, health is Kubernetes' job and job ownership stays in the Postgres lease, limits are global only and the rate limit is per download, a download running at the window end finishes.

## What worked


- DB tests against pg-dev: export `SAVETUBE_TEST_DATABASE_URL` in a subshell, with the password from `op read 'op://Homelab/savetube-dev-db/password'`. Take `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` first. URL-quote the password, and point `sslrootcert` at `~/.postgresql/postgres-dev-ca.crt`. No `.env` is needed.
- `SAVETUBE_ENV_FILE=<file> docker compose up` points the stack at another env file, without touching `.env`.
- Chart tests render with `helm template` and feed the rendered CronJob into the real `recorder_job()`.
- Off the home LAN, the Talos API answers only on the tailnet: `talosctl --talosconfig talos/generated/talosconfig -e talos-pve1.taila166d5.ts.net -n 192.168.33.20`. `make restore` refreshes that talosconfig. Ansible reaches pve1 with `ARGS='-e ansible_host=100.69.133.35'`.

- Local end-to-end check: a throwaway database `savetube_e2e` on pg-dev, `SAVETUBE_ARCHIVE_ROOT` under `/tmp`, `SAVETUBE_STATIC_DIR=frontend/dist`, and a uvicorn script that sets `app.dependency_overrides[current_user]` to skip login. Run `savetube scheduler` and `savetube worker` beside it, each with its own `SAVETUBE_METRICS_PORT` and `SAVETUBE_HEALTH_PORT`, because both default to 9090 and 8081. Set the rule format to `wa[ext=m4a]/wa` on this Mac.
- Rate limit check: `du -sk` of `.incoming/<job id>/` twice, 20 s apart. A limit of 50,000 bytes/s measured 39 KiB/s.
- Screenshots without the browser extension: Playwright through `uvx --with playwright python -I <script>` with `channel="chrome"`, which uses the installed Chrome.
- Off the home LAN, `kubectl --context homelab-tailnet` reaches the cluster. The postgres-dev CA comes from Secret `postgres-dev-ca` in `postgres-dev`.

## What did not work

- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`. `tests/test_auth.py` `FakeIssuer` patches `httpx2.AsyncHTTPTransport` instead.
- Without `relationship()`, SQLAlchemy does not order inserts by FK, so `tests/test_db.py` flushes parents first.
- `make talos-config` connects to the node's LAN address only, so it fails off the home LAN. A single new document goes in with `talosctl patch mc --mode no-reboot` over the tailnet, after a `--dry-run` that shows the diff.
- This Mac has no ffmpeg, so a local download that merges video and audio fails. A single-file format such as `wa[ext=m4a]/wa` works. YouTube serves no combined format for some videos, so `worst` fails too.
- `make tank ARGS='--check'` fails at the SMART report: check mode skips `smartctl`, and `from_json` gets empty output.

## Open questions

- Should yt-dlp get the `curl-cffi` extra? The worker logs "no impersonate target is available".
- Did a PO token reach a request? `bgutil:http-2.0.2` registered as available, but no log line shows a token being fetched.
- The image is 1.22 GB.
- The pot-provider sidecar runs without `readOnlyRootFilesystem`. Whether the image writes to its root filesystem is unchecked.
- The frontend bundle is 930 kB, and `frontend/README.md` is still the Vite template.
- The tests in `test_layout`, `test_ytdl`, `test_auth`, `test_k8s` and `test_db` were written beside the code, not test-first. A mutation pass would show what they miss.
- `docs/plan/operations.md` § Homelab deployment was meant to move to homelab-workloads before the repository went public. It holds hostnames and infrastructure layout, no secrets.

## Next steps

1. Log in through a browser at `https://savetube.lab.hiriri.com` as a user holding `savetube-admin`, add a source, start its downloads, and watch the first archive job in production. Check the worker and scheduler livenessProbes stay green.
2. Follow-ups:
   - `JobOut` omits `log_tail`, so the reclaim reason "worker stopped during the download" shows only in the database.
   - A new child's `last_polled_at` is backdated to spread polls, so the UI shows a "Last sync" time for a playlist never polled.
   - The UI cannot edit a source's poll interval.
   - Narrowing words does not cancel a queued job whose video another source also lists.
   - A word change does not cancel a running download, and turning `archiving` off cancels nothing.
   - The title preview returns every listed title with no pagination.
3. Review live recording with the user before building the recorder path.

## Suggested skills

`tdd` for new seams, `gh-cli` for workflow runs and repo checks.

## Pointers

- `docs/plan/README.md`: design index, requirements and Build order
- `charts/savetube/values.yaml`
- homelab-workloads `savetube/`
- `.env.sample` and `Makefile`
- homelab-platform `docs/deploying-to-kubernetes.md`
