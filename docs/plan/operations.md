# Operations

## Notifications and monitoring

- Live detected, recording started, recording finished, and recording failed post to a Discord webhook, the same kind Alertmanager uses (homelab-platform `docs/specs/20-observability.md`).
- `/metrics` exposes job counts by state and the last successful cookie check. Alert rules cover failed recorder Jobs and an expired cookie.
- A daily canary Job runs a short from-start capture of a known 24/7 public stream. It catches a yt-dlp break before a real live does.

## Homelab deployment

This section moves to homelab-workloads before the repository goes public.

- Build: a GitHub-hosted runner pushes the image to `ghcr.io/kocean33/savetube`, public like the repository. The lab dropped self-hosted runners, and GHCR needs no secret beyond `GITHUB_TOKEN`. The node pulls it through Harbor's `ghcr.io` pull-through cache, so no pull secret is needed.
- Versioning: development happens on `develop`, and a merge to main releases and deploys. main accepts only merged pull requests, with no bypass, because the project has one maintainer and every change to main ships to production. The deploy job sets `image.tag` in homelab-workloads to the version. python-semantic-release reads the Conventional Commits since the last tag: `feat` bumps minor, every other type bumps patch, a breaking change bumps major. Every type bumps because every merge ships, and a deployed image always carries a version. During 0.x a breaking change bumps minor, so 1.0.0 is a deliberate step. Git tags are the only version source, so no release commit lands on main and main never moves ahead of develop. Merge develop into main with a merge commit, because a squash leaves only the PR title for the version calculation.
- Deployment: the Helm chart `charts/savetube/` in this repository, because the scheduler depends on names the chart renders: the CronJob, the `recorder` container, the env variables. The Application is multi-source: the chart from this repository over HTTPS, the values from `homelab-workloads/savetube/envs/`, and the ExternalSecrets and static PV from `homelab-workloads/savetube/manifests/<env>/`. The release name is `savetube`, and the namespace carries the environment.
- The chart takes every secret as an existing Secret: the database URL, the session key, the optional OIDC client secret, the cookie file.
- `savetube-db` in the app namespace holds the whole libpq URL under `url`, templated from the vault password. A base64 password holds `/` and `+`, so the template escapes it with `urlquery`, or the URL splits at the `/`.
- Database: a `Database` object, a `managed.roles` entry, and ExternalSecret `savetube-db` on the shared Cluster (homelab-platform `docs/specs/10-postgres.md`). Development and the DB-gated tests use `pg-dev.lab.hiriri.com:5432` on `postgres-dev`, role `savetube` with CREATEDB, as saveweb does (`.env.sample`). The tests create and drop their own database, `savetube_test`.
- UI: `savetube.lab.hiriri.com` through the Gateway, tailnet and LAN only. Never Cloudflare Tunnel, because its terms forbid proxying video (proxmox-talos-lab `docs/specs/50-applications.md`).
- Storage: `tank/youtube` is a proxmox-talos-lab `pve_tank_datasets` entry with `talos: rw`, `owner: 1000` (the pod uid), `recordsize: 1M`. Talos mounts it at `/var/mnt/youtube`, and the static PV `savetube-archive` lives in homelab-workloads `savetube/manifests/prod/volumes.yaml`.
- `tank/video` cannot hold this. Samba is its one writer, and a dataset reaches the cluster only as its own virtiofs mapping (proxmox-talos-lab `docs/specs/20-storage.md`).
- The application writes `tank/youtube` directly, so no SMB trash covers it. A deleted channel or an unarchived live cannot be fetched again, so the dataset uses the `tight` snapshot template.
- A new virtiofs device needs a full stop and start of the Talos VM. The whole cluster goes down for that window.
- Size per hour of 1080p60 is a few GB and 4K is several times that. `tank` had 19.2T free when the dataset was created.
