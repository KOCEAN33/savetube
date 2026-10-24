# Operations

## Notifications and monitoring

- Live detected, recording started, recording finished, and recording failed post to a Discord webhook, the same kind Alertmanager uses (homelab-platform `docs/specs/20-observability.md`).
- `/metrics` exposes job counts by state and the last successful cookie check. Alert rules cover failed recorder Jobs and an expired cookie.
- A daily canary Job runs a short from-start capture of a known 24/7 public stream. It catches a yt-dlp break before a real live does.

## Logging

- The reader is the operator, through `kubectl logs` and k9s. Loki holds the same lines for 30 days (homelab-platform `docs/specs/20-observability.md`).
- Logs show what downloads now, how far it is, and why work waits or fails. Metrics and the UI hold history and totals.
- Every line is logfmt on stderr, such as `ts=2026-10-21T11:44:58+09:00 level=info event=job.start job=230 video=Kd9PnKbqdE4`.
- Logfmt reads as plain text in a terminal. Alloy reads `level=` from a logfmt line as the Loki level, and LogQL `| logfmt` makes each field a filter.
- `ts` is in Asia/Seoul, the zone of the download window.
- A savetube line names its event as `event=<noun>.<verb>`. A library line carries `logger=` and `msg=`.
- A line about a job carries `job` and `video`. A line that starts or ends a job, or reports a live, also carries `channel` and `title`. A poll line carries `source` and `url`.
- INFO is one line per state change: a poll, a dispatch, a change in why work waits, a job start, progress, end or failure, a live detection, a settings change.
- WARNING is a failure savetube handles itself, such as a failed poll, a failed attempt before the third, or a reclaimed job.
- ERROR needs the operator: a third failed archive attempt, any failed recording, or an unexpected exception.
- Only an unexpected exception logs a traceback. An expected failure is one line with `reason=`.
- DEBUG holds loop ticks that change nothing, yt-dlp's screen output, and the access log.
- `SAVETUBE_LOG_LEVEL` sets the level, and the default is `INFO`. The chart sets it from `logLevel`.
- Libraries log at WARNING and above. At `DEBUG` they log at INFO and above. Alembic logs at INFO, so the migrate container shows each revision it applies.
- The uvicorn access log is off below `DEBUG`. The probes hit `/healthz` every few seconds, and the API logs its own writes.
- yt-dlp logs through `savetube.ytdlp`. Its warnings stay WARNING. Its errors log at DEBUG, because the caller logs the same text as `reason=`.
- The download child process configures its own logging and builds its own hooks, because `spawn` pickles its arguments.
- A download logs `download.progress` at most once a minute, with percent, bytes, speed and ETA of the current stream. A time interval bounds both the delay and the volume of progress lines for any file size or rate limit.
- The progress hook fires only when bytes arrive. A download with no progress line for several minutes, and no `download.merge`, is stalled.
- A live recording has no total, so its progress line shows bytes, fragments and elapsed time.
- The scheduler logs `dispatch.waiting` when the reason work waits changes: no free slot, a YouTube block pause, the daily cap, with `today=` the count and the cap, or the window opens at a later time. It logs `queue.summary` every 10 minutes while a job waits or runs, with `blocked=` the pause end or `none`.
- A YouTube block logs `dispatch.blocked` at WARNING when it starts a pause. The first archive success after the pause logs `dispatch.unblocked` at INFO.
- The user's pause switch logs `dispatch.paused` or `dispatch.resumed` at INFO with `running`, the running archive downloads, only when the value changes. A running download whose pause check fails logs `job.pause_check_failed` at WARNING and goes on.
- A run whose job row a reclaim took logs `job.disowned` at WARNING with `job`, `video` and `reason`, and writes nothing more (archive.md, Download limits).
- A staging move logs `staging.move` at INFO with `reason` (`batch`, `space`, `idle` or `manual`), the count and the size, and `staging.moved` with the count moved and the time it took. A video that fails to move logs `staging.move_failed` at ERROR and waits for the next check. Move now logs `staging.move_requested` from the web.
- The hourly database cleanup logs `cleanup.done` at INFO with the counts `progress`, `queue_jobs` and `interrupted`, only when one is above 0.
- `poll.done` counts the listed entries, the queued jobs, and each skip reason: words, members-only without a member profile, another channel, date, live, private.
- A membership check logs `membership.checked` at INFO with the channel, the profile, the video, and `member` yes or no. A check that ends in neither logs `membership.check_failed` at WARNING with the reason, and the previous result stays.
- An account read logs `cookies.account` at INFO with the profile, the account name and the handle. A failed read logs `cookies.account_failed` at WARNING with the profile and the reason, with a traceback only for an unexpected error. A replaced file logs `cookies.changed` at INFO with the profile and `memberships`, the deleted membership results.
- `job.start` logs `cookies` as the profile name, or `none`. No log line holds cookie content.

## Homelab deployment

This section moves to homelab-workloads before the repository goes public.

- Build: a GitHub-hosted runner pushes the image to `ghcr.io/kocean33/savetube`, public like the repository. The lab dropped self-hosted runners, and GHCR needs no secret beyond `GITHUB_TOKEN`. The node pulls it through Harbor's `ghcr.io` pull-through cache, so no pull secret is needed.
- Versioning: development happens on `develop`, and a merge to main releases and deploys. main accepts only merged pull requests, with no bypass, because the project has one maintainer and every change to main ships to production. The deploy job sets `image.tag` in homelab-workloads `savetube/envs/prod.yaml` to the version and `chartRevision` in `savetube/appset.yaml` to the release tag `v<version>`, in one commit. The chart and the image then always come from one release, because a new chart on an old image can fail, as a livenessProbe on an image without `/healthz` did. Nobody edits either value by hand. python-semantic-release reads the Conventional Commits since the last tag: `feat` bumps minor, every other type bumps patch, a breaking change bumps major. Every type bumps because every merge ships, and a deployed image always carries a version. During 0.x a breaking change bumps minor, so 1.0.0 is a deliberate step. Git tags are the only version source, so no release commit lands on main and main never moves ahead of develop. Merge develop into main with a merge commit, because a squash leaves only the PR title for the version calculation.
- Deployment: the Helm chart `charts/savetube/` in this repository, because the scheduler depends on names the chart renders: the CronJob, the `recorder` container, the env variables. The Application is multi-source: the chart from this repository over HTTPS, the values from `homelab-workloads/savetube/envs/`, and the ExternalSecrets and static PV from `homelab-workloads/savetube/manifests/<env>/`. The release name is `savetube`, and the namespace carries the environment.
- The chart takes every secret as an existing Secret: the database URL, the session key, the optional OIDC client secret, the cookie profiles. The cookie Secret mounts as the directory `SAVETUBE_COOKIES_DIR` in the scheduler, worker and recorder pods. The web pod never mounts it.
- The chart renders two Services: `savetube-web` (HTTP and metrics) for the Gateway, and the ClusterIP `savetube-scheduler` on port 8082 for the scheduler's internal API, which only the web calls (runtime.md, Scheduler API).
- The scheduler, worker and recorder pods mount the archive claim read-write at `/archive`. The web pod mounts the same claim read-only for playback, with both the volume and the mount marked `readOnly`. The claim is a `local` PV pinned to one node, so every pod that mounts it runs on that node.
- `savetube-db` in the app namespace holds the whole libpq URL under `url`, templated from the vault password. A base64 password holds `/` and `+`, so the template escapes it with `urlquery`, or the URL splits at the `/`.
- Database: a `Database` object, a `managed.roles` entry, and ExternalSecret `savetube-db` on the shared Cluster (homelab-platform `docs/specs/10-postgres.md`). Development and the DB-gated tests use `pg-dev.lab.hiriri.com:5432` on `postgres-dev`, role `savetube` with CREATEDB, as saveweb does (`.env.sample`). Each test run creates and drops its own database, `savetube_test_<random>`, so concurrent runs do not collide. A killed run leaves its database behind.
- UI: `savetube.lab.hiriri.com` through the Gateway, tailnet and LAN only. Never Cloudflare Tunnel, because its terms forbid proxying video (proxmox-talos-lab `docs/specs/50-applications.md`).
- Storage: `tank/youtube` is a proxmox-talos-lab `pve_tank_datasets` entry with `talos: rw`, `owner: 1000` (the pod uid), `recordsize: 1M`. Talos mounts it at `/var/mnt/youtube`, and the static PV `savetube-archive` lives in homelab-workloads `savetube/manifests/prod/volumes.yaml`.
- `tank/video` cannot hold this. Samba is its one writer, and a dataset reaches the cluster only as its own virtiofs mapping (proxmox-talos-lab `docs/specs/20-storage.md`).
- The application writes `tank/youtube` directly, so no SMB trash covers it. A deleted channel or an unarchived live cannot be fetched again, so the dataset uses the `tight` snapshot template.
- A new virtiofs device needs a full stop and start of the Talos VM. The whole cluster goes down for that window.
- Size per hour of 1080p60 is a few GB and 4K is several times that. `tank` had 19.2T free when the dataset was created.
