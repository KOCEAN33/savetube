# savetube plan

Draft for archiving chosen YouTube channels and playlists, recording lives from their first second, and managing both from one web UI. Not built.

## Requirements

1. Back up chosen playlists and whole channels: videos, Shorts, and past lives, at the best quality YouTube serves.
2. Download members-only content with the user's own account cookies, per channel.
3. Watch chosen channels and record a live the moment it starts. An unarchived live is gone once it ends.
4. One web UI to add channels and playlists, set per-source rules, browse stored videos and their metadata, and see storage statistics.
5. A structured tree on `tank`: `<channel>/<type>/<video>`, never a flat directory.
6. Runs in Kubernetes. Download work uses published libraries, not hand-written extractors.

"Original quality" means the best stream YouTube serves. YouTube never serves the uploader's source file.

## Survey (2026-10)

No existing application meets 1-5.

| Tool | Fits | Disqualifier |
| --- | --- | --- |
| yt-dlp | engine for everything: `--live-from-start`, `--wait-for-video`, cookies, output templates with directories | no UI, no scheduler |
| ytarchive | was the standard live recorder | broken in 2026: open issues on from-start and "No download URLs found", avtdl's README calls it broken |
| hoshinova | channel watcher with a status page | wraps ytarchive, last release 2023 |
| LiveStreamDVR, Ganymede | live recorders with UIs | Twitch-centred, Ganymede is Twitch-only |
| avtdl | YouTube watcher with web UI and member-stream monitoring | only runs commands, 37 stars, no dated releases |
| Tube Archivist | mature archive UI | Elasticsearch and Redis, fixed `<channel-id>/<video-id>.mp4` layout, no live recording |
| Pinchflat | per-source rules, path templates, cookies | SQLite only, maintenance-only since 2025-09, no live recording |
| Pinchflat-NGX | Pinchflat fork with PostgreSQL 18, OIDC, cookie editor | 205 stars, one maintainer, live recording not described |
| TubeSync | PostgreSQL, active | fixed `video`/`audio` layout, no live recording |
| ytdl-sub | YAML presets express the layout well | the "UI" is a code-server editor |
| MeTube, Youtarr | active UIs with subscriptions | no live recording, Youtarr is MariaDB |

Requirement 3 needs custom code under every option. The custom part is a watcher, a recorder, a database, and a UI for both.

## yt-dlp facts the design rests on

- `--live-from-start` downloads the DASH segments from the stream's first second. It is marked experimental and broke at least four times in 2026 (yt-dlp issues 15751, 15274, 16237, 15921).
- From-start works only while the DVR window holds the start. The window is 120 hours. A stream with DVR disabled records from the join time only.
- So detection latency costs nothing on a DVR stream that is still running. It costs footage on a DVR-off stream, and the whole stream if it ends before detection.
- yt-dlp needs a JS runtime since 2025.11.12. Deno is the default, minimum 2.3.0. A pip install also needs a matching `yt-dlp-ejs`.
- `bgutil-ytdlp-pot-provider` supplies PO tokens through an HTTP server on port 4416. It has no auth, so it binds to `127.0.0.1`.
- With cookies, some accounts get "No video formats found!" on from-start (issue 15274). Public lives keep a cookie-free path.
- `live_status` and `availability` (`subscriber_only`) are output-template and info-dict fields, so the type directory is computable per video.
- The yt-dlp wiki puts the limits near 300 videos/hour as a guest and 2000/hour logged in, and warns that a logged-in account risks a temporary or permanent ban. The `-t sleep` preset is `--sleep-requests 0.75 --sleep-interval 10 --max-sleep-interval 20 --sleep-subtitles 5`.

## Architecture

One custom Python application embeds yt-dlp as a library. Sources, rules, jobs, and metadata live in one Postgres database, and one UI covers archives, lives, and statistics.

Requirement 3 needs a custom recorder, database, and UI under any option, so the archive downloader on top costs one queue and one yt-dlp call.

Rejected:

- Pinchflat-NGX for archives beside a custom recorder: two UIs, two rule sets, dedupe between them, and a one-maintainer fork.
- Pinchflat-NGX with avtdl driving yt-dlp: three UIs, and recorded lives appear in no browse UI.

## Drafted shape

### Components

One image, three roles, plus a recorder Job per live.

| Role | Kind | Does |
| --- | --- | --- |
| `web` | Deployment | API and the built frontend |
| `scheduler` | Deployment, 1 replica | polls sources on their schedules, detects lives, creates recorder Jobs |
| `worker` | Deployment | archive downloads from the Postgres queue |
| recorder | Job per live | `--live-from-start` on one video, then post-processing |

- A recorder runs as its own Job so a rollout of `web`, `scheduler`, or `worker` never cuts a recording. The scheduler needs a Role allowed to create and list Jobs in its namespace.
- A recorder that dies retries through the Job's `backoffLimit`. The retry restarts from the first segment while the DVR window lasts.
- Every pod that runs yt-dlp carries the bgutil provider as a native sidecar bound to `127.0.0.1:4416`.
- Backend: Python with FastAPI. The API, scheduler, worker, and recorder share one package.
- Queue: `procrastinate`, a Python job queue on Postgres. No Redis.

### Embedding yt-dlp

- `yt_dlp.YoutubeDL` runs in-process. Progress arrives through `progress_hooks`, and `extract_info` returns the info dict without JSON parsing.
- One `YoutubeDL` instance per download. An instance is never shared between concurrent downloads.
- Each archive download runs in its own child process. A Python thread cannot be stopped from outside, and a cancel or a hung download needs a process kill. A recorder is already one process in its own pod.
- `yt-dlp[default]` pins the matching `yt-dlp-ejs`. yt-dlp 2026.8.19 pins `yt-dlp-ejs==0.8.0` and needs Python 3.10 or later.
- `bgutil-ytdlp-pot-provider` installs from PyPI as a yt-dlp plugin. The plugin is the client; the provider server runs as the sidecar.
- yt-dlp, `yt-dlp-ejs`, and the plugin sit in one lockfile. Renovate bumps them, and the image rebuilds on each bump.
- The image adds Deno and ffmpeg. The base image is glibc, because Deno does not support musl.

### Frontend

- React with shadcn/ui, starting from the `dashboard-01` block: sidebar, section cards, an interactive area chart, and a data table. `npx shadcn add dashboard-01` installs it.
- Vite builds a static bundle that FastAPI serves as static files. The block ships as Next.js page files, so its components move into a Vite app.
- Pages: dashboard, channels and sources, rules, jobs and live recordings, video browser with metadata.

### Statistics

The dashboard reads Postgres. No second metrics store backs it.

| Widget | Source |
| --- | --- |
| Total stored size, video count, hours archived | `sum(size_bytes)`, `count(*)`, `sum(duration)` over `video` |
| Size and count by channel and by type | the same, grouped by `channel_id`, `type` |
| Bytes added per day or week | `video.completed_at` bucketed with `date_trunc` |
| Live recordings: recorded, failed, missed | `job` where kind is record, grouped by state |
| Queue: pending, running, failed | procrastinate's job table |
| Dataset free space | `statfs` on the mount, sampled into a table hourly |
| Last sync per source | `source.last_polled_at`, `source.last_error` |

- `video.size_bytes` sums every file in the video's directory, so subtitles and thumbnails count.
- A missed live is a detected stream whose recorder never captured from the first segment. It carries the reason: DVR off, ended before detection, or recorder failure.

### Data model

| Table | Holds |
| --- | --- |
| `channel` | channel id, handle, display name, the directory name fixed at first add |
| `source` | a channel tab or a playlist URL, enabled flag, poll interval, rule id, last poll time and error |
| `rule` | content types to keep, date cutoff, format selector, use cookies, watch lives, max concurrent |
| `video` | video id, channel, type, title, upload date, duration, live status, availability, format ids, file paths, `size_bytes`, `completed_at`, state |
| `video_source` | which sources reference a video, so a playlist never copies a file |
| `job` | kind (archive, record), state, attempts, miss reason, log tail, timestamps |
| `storage_sample` | hourly free and used bytes of the dataset |
| `video.info` | the full yt-dlp info dict as JSONB |

- The `.info.json` beside each video is the source of truth. A rescan of the tree rebuilds `video` without network calls.
- A playlist is a relation, not a directory. A video sits once under its uploader's channel, and the UI shows the playlist view.

### Layout

New dataset `tank/youtube`, mounted read-write in Talos. Inside it:

```
<channel name> [<channel id>]/
  videos/  <YYYY-MM-DD> <title> [<video id>]/
  shorts/  <YYYY-MM-DD> <title> [<video id>]/
  lives/   <YYYY-MM-DD> <title> [<video id>]/
  members/ <YYYY-MM-DD> <title> [<video id>]/
.incoming/<job id>/
```

- Each video directory holds the media, `.info.json`, the thumbnail, subtitles, and the chat replay when YouTube offers one.
- Type rule, first match wins: `availability == subscriber_only` is `members`. A `was_live` or recorded live is `lives`. A video from the `/shorts` tab is `shorts`. The rest is `videos`.
- The channel directory name is fixed at first add and stored in `channel`. A later channel rename changes the display name only, never the path.
- Downloads land in `.incoming/<job id>/` on the same dataset, so the final move is a rename and a half-written file never appears in the tree.
- Titles pass through yt-dlp's filename sanitising and a length cap, because a title can exceed the 255-byte name limit in UTF-8.
- Format: `bv*+ba/b`, merged to `mkv`, which holds any codec pair YouTube serves.

### Live detection

- Every watched channel's RSS feed, polled every few minutes, finds new and upcoming video ids. RSS needs no quota.
- Data API `videos.list` with `liveStreamingDetails` reads `scheduledStartTime` and `actualStartTime` for up to 50 ids per call at 1 unit. The default allocation is 10,000 units/day.
- An upcoming stream gets a recorder Job a few minutes before `scheduledStartTime`, running with `--wait-for-video` and `--live-from-start`.
- A stream with no waiting room appears in neither feed in time. The scheduler also polls each watched channel's `/live` page through yt-dlp extraction, about once a minute, for watched channels only.
- WebSub push needs a public HTTPS callback, which the tailnet-only cluster lacks. Not used.

### Cookies and account

- The cookie file is one 1Password item, delivered by an ExternalSecret (proxmox-talos-lab ADR secrets) and copied into an `emptyDir` per run, because yt-dlp writes the cookie jar back.
- Only sources whose rule sets "use cookies" pass the file. Public sources run cookie-free with PO tokens.
- Export cookies from a private window that is then closed, because YouTube rotates cookies on open tabs.
- Requests run with the `-t sleep` intervals and a global concurrency cap across `worker` and recorders.

### Notifications and monitoring

- Live detected, recording started, recording finished, and recording failed post to a Discord webhook, the same kind Alertmanager uses (homelab-platform `docs/specs/20-observability.md`).
- `/metrics` exposes job counts by state and the last successful cookie check. Alert rules cover failed recorder Jobs and an expired cookie.
- A daily canary Job runs a short from-start capture of a known 24/7 public stream. It catches a yt-dlp break before a real live does.

## Homelab deployment

This section moves to homelab-workloads before the repository goes public.

- Build: the in-cluster ARC runner pushes the image to a new Harbor project, like lrr-reader.
- Deployment: `homelab-workloads/savetube/appset.yaml`, Kustomize shape.
- Database: a `Database` object, a `managed.roles` entry, and ExternalSecret `savetube-db` on the shared Cluster (homelab-platform `docs/specs/10-postgres.md`). Development uses `postgres-dev`.
- UI: `savetube.lab.hiriri.com` through the Gateway, tailnet and LAN only. Never Cloudflare Tunnel, because its terms forbid proxying video (proxmox-talos-lab `docs/specs/50-applications.md`).
- Storage: a `pve_tank_datasets` entry with `talos: rw`, an `owner` uid matching the pod, `recordsize: 1M`. The static PV copies homelab-services `manifest/lanraragi/volumes.yaml`.
- `tank/video` cannot hold this. Samba is its one writer, and a dataset reaches the cluster only as its own virtiofs mapping (proxmox-talos-lab `docs/specs/20-storage.md`).
- The application writes `tank/youtube` directly, so no SMB trash covers it. A deleted channel or an unarchived live cannot be fetched again, which argues for the `tight` snapshot template.
- A new virtiofs device needs a full stop and start of the Talos VM. The whole cluster goes down for that window.
- Size per hour of 1080p60 is a few GB and 4K is several times that. Free space on `tank` was not checked: pve1 did not answer SSH during this survey.

## Unsettled

- Whether `statfs` through virtiofs reports the dataset's free space or the pool's.
- Which Google account holds the memberships. Using the main account puts it at ban risk. A separate account needs its own memberships.
- Whether members-only uploads and lives appear in RSS and `videos.list`. If not, the `/live` poll and the members playlist need cookies. The `UUMO` playlist-id prefix for members-only uploads is third-party lore, not checked.
- Whether issue 15274's fix (PR 16771) is in a release.
- A read-only Samba mount of `tank/youtube` for watching on a PC. Samba stays a reader, so the dataset keeps one writer.
- Live chat capture during a live, not only replay. Needs a separate library. Not surveyed.
- Offsite backup: none of the current tiers can carry this size.
- UI login: tailnet only, or Zitadel OIDC on top.

## Build order

1. Check `tank` free space and decide the dataset's snapshot template.
2. Create the Harbor project and the ARC build. Prove an image with Python, Deno, ffmpeg, yt-dlp and the bgutil plugin runs one from-start capture of a public live in the cluster through `YoutubeDL`.
3. Prove a members-only download and a members-only live with the chosen account's cookies.
4. Build the recorder Job and the scheduler's live detection, with Discord notifications. Live recording is the requirement nothing else covers.
5. Build the archive worker, the queue, and the tree layout.
6. Build the UI from `dashboard-01`: statistics, sources and rules, jobs, the video browser.
7. Add `tank/youtube` to the lab inventory, stop and start the Talos VM, then add the static PV.
8. Ask for permission to add the role and `Database` to the shared Cluster `postgres`, then deploy through homelab-workloads.
