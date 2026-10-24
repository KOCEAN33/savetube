# Runtime

## Components

One image, three roles, plus a recorder Job per live.

| Role | Kind | Does |
| --- | --- | --- |
| `web` | Deployment | API and the built frontend |
| `scheduler` | Deployment, 1 replica | polls sources on their schedules, detects lives, creates recorder Jobs, serves the internal API |
| `worker` | Deployment | archive downloads from the Postgres queue |
| recorder | Job per live | `--live-from-start` on one video, then post-processing |

- A recorder runs as its own Job so a rollout of `web`, `scheduler`, or `worker` never cuts a recording. The scheduler needs a Role allowed to get CronJobs and to create and list Jobs in its namespace. The web role has a read-only Role, get and list on pods and Jobs in its namespace, for the Live page's recorder state, so its pod mounts a ServiceAccount token. The worker and recorder pods mount none.
- The recorder's pod spec is a suspended CronJob, `savetube-recorder`. The scheduler copies its `jobTemplate` into a Job named `savetube-recorder-<job id>`, the way `kubectl create job --from=cronjob/...` does, with an ownerReference to the CronJob so Argo CD shows the Job as a child instead of pruning it. The image, sidecar and volumes stay in the manifests, and a video id never enters a name, because it holds upper case and `_`.
- `savetube migrate` runs as an init container of `scheduler`, the one single-replica role, so two migrations never race. It installs procrastinate's schema on an empty database, then runs alembic. Autogenerate skips `procrastinate_*` tables. A procrastinate upgrade that adds SQL migrations needs an alembic revision applying them.
- A recorder that dies retries through the Job's `backoffLimit`. The retry restarts from the first segment while the DVR window lasts.
- Every pod that runs yt-dlp carries the bgutil provider as a native sidecar bound to `127.0.0.1:4416`.
- Backend: Python 3.14 with FastAPI, SQLAlchemy 2 async on psycopg 3, alembic. uv manages the project and `uv.lock`. The API, scheduler, worker, and recorder share one package and one CLI, `savetube <role>`.
- Queue: `procrastinate`, a Python job queue on Postgres. No Redis.
- The database address is PgBouncer in transaction mode. psycopg never prepares statements (`prepare_threshold=None`), and the worker polls every 5 s with `listen_notify` off, because a LISTEN does not survive the pooler.
- Every role serves Prometheus metrics on port 9090, apart from the HTTP port.

## Scheduler API

- A user action that asks the scheduler to act now goes over HTTP, never through a DB flag the scheduler loop picks up.
- The scheduler serves a FastAPI app (`savetube.scheduler_api`) on port 8082 (`SAVETUBE_SCHEDULER_API_PORT`) through uvicorn's `Server.serve()`, one task of the scheduler's asyncio TaskGroup. `GET /healthz` stays on the hand-written server on 8081, which the worker shares.
- The chart's ClusterIP Service `savetube-scheduler` exposes port 8082. The web calls it at `SAVETUBE_SCHEDULER_URL`, `http://savetube-scheduler:8082` in the chart and `http://scheduler:8082` in `compose.yaml`. The scheduler runs one replica with `Recreate`, so the Service reaches one pod.
- No auth: the API is cluster-internal and only the web calls it. The chart has no NetworkPolicy yet, so any pod in the cluster can reach it.
- On SIGTERM uvicorn closes the server, waiting up to 5 s for a running request, then raises the signal again, so the process stops as it did without the server.

| Route | Does |
| --- | --- |
| `POST /probe/{video_id}` | probes a `complete` video's formats now and stores them in `format_watch` (live.md, Format watch). It waits for the format watch's probe in progress. 204 when stored, a failed probe included, 404 for an unknown video, 409 for one not `complete` |

## Embedding yt-dlp

- `yt_dlp.YoutubeDL` runs in-process. Progress arrives through `progress_hooks`, and `extract_info` returns the info dict without JSON parsing.
- One `YoutubeDL` instance per download. An instance is never shared between concurrent downloads.
- Each archive download runs in its own child process. A Python thread cannot be stopped from outside, and a cancel or a hung download needs a process kill. A recorder is already one process in its own pod.
- `yt-dlp[default]` pins the matching `yt-dlp-ejs`. yt-dlp 2026.8.19 pins `yt-dlp-ejs==0.8.0` and needs Python 3.10 or later.
- `bgutil-ytdlp-pot-provider` installs from PyPI as a yt-dlp plugin. The plugin is the client; the provider server runs as the sidecar.
- yt-dlp, `yt-dlp-ejs`, and the plugin sit in one lockfile. Renovate bumps them, and the image rebuilds on each bump.
- The image adds Deno and ffmpeg. The base image is glibc, because Deno does not support musl.
