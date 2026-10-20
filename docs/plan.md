# savetube plan

Draft for archiving chosen YouTube channels and playlists, recording lives from their first second, and managing both from one web UI. Built: web with login, the UI and API for sources and rules, source polling, live detection, the archive worker, the storage pipeline, the image. Live recording is not built.

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

- A recorder runs as its own Job so a rollout of `web`, `scheduler`, or `worker` never cuts a recording. The scheduler needs a Role allowed to get CronJobs and to create and list Jobs in its namespace.
- The recorder's pod spec is a suspended CronJob, `savetube-recorder`. The scheduler copies its `jobTemplate` into a Job named `savetube-recorder-<job id>`, the way `kubectl create job --from=cronjob/...` does, with an ownerReference to the CronJob so Argo CD shows the Job as a child instead of pruning it. The image, sidecar and volumes stay in the manifests, and a video id never enters a name, because it holds upper case and `_`.
- `savetube migrate` runs as an init container of `scheduler`, the one single-replica role, so two migrations never race. It installs procrastinate's schema on an empty database, then runs alembic. Autogenerate skips `procrastinate_*` tables. A procrastinate upgrade that adds SQL migrations needs an alembic revision applying them.
- A recorder that dies retries through the Job's `backoffLimit`. The retry restarts from the first segment while the DVR window lasts.
- Every pod that runs yt-dlp carries the bgutil provider as a native sidecar bound to `127.0.0.1:4416`.
- Backend: Python 3.14 with FastAPI, SQLAlchemy 2 async on psycopg 3, alembic. uv manages the project and `uv.lock`. The API, scheduler, worker, and recorder share one package and one CLI, `savetube <role>`.
- Queue: `procrastinate`, a Python job queue on Postgres. No Redis.
- The database address is PgBouncer in transaction mode. psycopg never prepares statements (`prepare_threshold=None`), and the worker polls every 5 s with `listen_notify` off, because a LISTEN does not survive the pooler.
- Every role serves Prometheus metrics on port 9090, apart from the HTTP port.

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
| `channel` | channel id, handle, display name, the directory name fixed at first add, include and exclude title words |
| `global_words` | one row: the global include and exclude title words |
| `source` | a channel tab, a channel's playlists, or a playlist URL, enabled flag, poll interval, rule id, include and exclude title words, other-channel flag, parent source, last poll time and error |
| `listing_entry` | every video a source's last listings showed: source, video id, channel, title. Title word suggestions read it |
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
- Files inside a video directory are named `<video id>.<ext>`, so only directory names carry a title and need the byte cap.
- yt-dlp's `.tmp` fragments directory is deleted before the rename. The cookie copy lives in a private temporary directory outside `.incoming/`, so it never moves into the tree.

### Source polling

- A source is one channel tab (`videos`, `shorts`, `streams`), one channel's playlists (`playlists`), or one playlist, stored as a canonical URL. Adding a bare channel URL creates one source per chosen tab.
- The scheduler lists a due source with yt-dlp's flat extraction: ids and titles, no request per video. Listing a channel with a few thousand videos takes minutes, because every page request sleeps 0.75 s.
- A tab's entries carry no `channel_id`, so the poll takes it from the listing. Playlist entries carry their own, and a private or deleted entry without one is skipped.
- Every listed video that has a `video` row gets a `video_source` link, whether or not it passes the filters. A video that passes and is not yet stored gets a `pending` `video` row and an archive job, unless a job for it is queued or running, or three archive jobs for it have failed.
- `is_live`, `is_upcoming` and `post_live` entries are left to live detection.
- The rule's date cutoff reads the entry's approximate timestamp ("3 weeks ago"), so it is only as exact as YouTube's relative date. A video uploaded a day before the cutoff can pass.
- A failed archive job deletes its `.incoming/<job id>/`, because the retry is a new job with a new directory.
- Every write to `/api/*` needs an `X-Requested-With` header. A cross-origin page cannot send it without a CORS preflight, and SameSite=lax still sends the cookie on a POST from a sibling `hiriri.com` subdomain.

### Channel playlists

- Two modes. "Track all" is one source of kind `playlists` at `https://www.youtube.com/<channel>/playlists`. "Pick some" is one `playlist` source per chosen playlist.
- A `playlists` source downloads nothing itself. Each poll lists the channel's playlists and creates a child `playlist` source for every playlist URL that is not a source yet and not in the parent's `ignored_urls`. A child stores `parent_id` and starts with `last_polled_at` set to a random point within its interval, so new children do not all fall due at once. Deleting the parent deletes its children.
- A child copies the parent's rule, poll interval and other-channel flag at creation. A later change of those on the parent is copied to every child. A child is skipped while its parent is disabled.
- Deleting a child adds its URL to the parent's `ignored_urls`, so the next parent poll does not create it again. Removing the URL from `ignored_urls` restores it on the next parent poll.
- A child whose playlist no longer appears in a successful, non-empty parent listing is disabled with `last_error` "playlist no longer listed on the channel".
- Each playlist stays its own source, so `video_source` keeps playlist membership for the playlist view.
- `source.other_channels`, default off, decides whether a playlist source archives videos uploaded by other channels. Off keeps only entries whose `channel_id` equals the source's channel. A child always keeps its parent's channel. A playlist source without a parent takes its channel from the listing's `channel_id`. With `other_channels` off and no channel, the poll queues nothing and sets `last_error`.

### Title words

- Word lists exist at three levels: global, channel, and source. Each holds `include_words` and `exclude_words`.
- An entry's effective include list is the union of the global list, the list of the entry's uploader channel, the parent source's list for a child, and the source's own list. The effective exclude list is the union of the same exclude lists.
- A title passes when the effective include list is empty or any word in it is in the title, and no effective exclude word is in the title. Matching is a case-insensitive substring test on casefolded text.
- Example: global include `歌枠` keeps `歌枠` titles on every channel. A channel that also wants covers adds `Cover` to its own include list. A source with no words at any level keeps every title.
- The archive poll and live detection both apply the effective lists. A detected live queues a `record` job only when its title passes the effective lists of at least one watching source: an enabled tab source of that channel whose rule watches lives. When such a source exists on the `streams` tab, only `streams` sources count.
- Live detection caches each probe's result per process and applies the words on every tick, so a word change takes effect without a restart.
- A poll cancels a queued archive job, and its procrastinate job, for a listed video that no longer passes. A running job continues.
- A list holds at most 200 words of up to 100 characters each.
- The global lists live in the single-row table `global_words`. The channel lists are columns on `channel`.
- The poll replaces the source's `listing_entry` rows with the full listing before any filtering, so suggestions and the preview see titles the words skip.
- A suggestion is a word that recurs in one channel's titles. The extractor takes the text inside `【】`, `[]`, `()`, `（）`, `「」` and each `#hashtag`. Only the outermost bracket counts, so `【ゲリラ(豪雨)】` gives `ゲリラ(豪雨)`. A word is casefolded with a leading `#` stripped, so `Cover` and `#cover` are one word, shown in its most frequent original form without `#`.
- Ranking counts distinct videos of the channel that carry the word. A word on one video only is dropped. The top 30 are returned.

### API for playlists and title words

| Method and path | Body or query | Returns |
| --- | --- | --- |
| `POST /api/sources` | `tabs` also accepts `playlists`. New fields `include_words`, `exclude_words` (lists of strings, default empty) and `other_channels` (default false) | the created sources |
| `PATCH /api/sources/{id}` | also `include_words`, `exclude_words`, `other_channels`, `ignored_urls` | the source |
| `GET /api/sources` | | each source also carries `include_words`, `exclude_words`, `other_channels`, `parent_id`, `ignored_urls` |
| `POST /api/channel-playlists` | `{url}`, a channel URL | `[{id, title, url, source_id}]`, `source_id` null for a playlist that is not a source yet. Lists live through yt-dlp, at most two at a time, 502 on a yt-dlp error, 504 after 120 s |
| `GET /api/channels/{id}/words` | | `[{word, count}]`, best first |
| `POST /api/sources/{id}/title-preview` | `{include_words, exclude_words}`, the source's own lists being edited | `{total, kept, titles: [{video_id, title, kept}]}` from the source's `listing_entry` rows, matched with the stored global and channel lists merged in |
| `GET /api/words` | | the global `{include_words, exclude_words}` |
| `PUT /api/words` | `{include_words, exclude_words}` | the global lists |
| `GET /api/channels` | | each channel also carries `include_words`, `exclude_words` |
| `PATCH /api/channels/{id}` | `{include_words?, exclude_words?}` | the channel |

- Word lists are trimmed, empty strings dropped, and duplicates removed after casefolding, keeping the first form.

### Listing before downloading

- A source added through the API starts with `archiving` off. Its polls write `listing_entry` rows and `video_source` links but create no `video` rows and no archive jobs, so the user can set words against the preview first.
- `PATCH /api/sources/{id}` with `archiving: true` starts downloads, and the source is polled on the next scheduler tick. On a `playlists` parent the flag is copied to every child, and a child created later takes the parent's value.
- Sources that existed before migration 0003 keep downloading: the migration sets `archiving` on for them.
- Live detection ignores `archiving`.

### Download limits

- One global row, `download_settings`, holds three limits. They apply to archive jobs only. A live recording ignores all three, because a live missed is lost.
- `max_concurrent`, 1 to 10, default 1: archive downloads running at once. The chart's `worker.concurrency` is the hard ceiling and defaults to 10, so the setting decides.
- `rate_limit_bytes`, null for no limit: passed to yt-dlp as `ratelimit`, so it limits each download. Three downloads at once can use three times the rate.
- `window_start` and `window_end`, `HH:MM` in Asia/Seoul, both null for no window: archive downloads start only inside the window. A window may cross midnight, such as 23:00-05:00. Equal start and end is rejected.
- A download already running when the window closes runs to the end.
- An archive task that starts outside the window, or with no free slot, defers its procrastinate job and leaves the `job` row `queued`. Outside the window it defers to the next window start. Without a free slot it defers 60 s. `job.deferred_until` shows the time. The slot check takes a Postgres advisory lock, so two workers cannot both take the last slot.
- `rule.max_concurrent` is dropped.

| Method and path | Body | Returns |
| --- | --- | --- |
| `GET /api/download-settings` | | `{max_concurrent, rate_limit_bytes, window_start, window_end, timezone}` |
| `PUT /api/download-settings` | `{max_concurrent, rate_limit_bytes, window_start, window_end}` | the settings |
| `GET /api/jobs` | | each job also carries `deferred_until` |
| `PATCH /api/sources/{id}` | also `archiving` | the source, with `archiving` |

### Live detection

- A watched channel is the channel of an enabled tab source whose rule has "watch lives" on.
- Every minute, per watched channel, the scheduler reads three places: the RSS feed, the first page of the Live tab, and the `/live` page.
- The RSS feed holds only the 15 newest uploads. On a busy channel a waiting room drops out of it days before the stream, so the Live tab's `is_upcoming` and `is_live` entries are candidates too.
- Each candidate id is probed once per scheduler process with yt-dlp and `ignore_no_formats_error`. Without that option an upcoming stream raises "This live event will begin in ..." instead of returning `release_timestamp`. A probe that fails, such as a members-only video without cookies, is skipped.
- The `/live` page finds a stream with no waiting room. "The channel is not currently live" is the normal answer and is not an error.
- No Data API key is used. The probe reads the scheduled start that `videos.list` would.
- A detection is a queued `record` job. `scheduled_at` comes from `release_timestamp` for an upcoming stream and is null for a stream found live. The record job keeps a video to one detection across scheduler restarts.
- The first check of a channel probes up to 15 feed ids and takes about a minute. Later checks take seconds.
- A queued record job does not stop the archive poll from queuing the VOD. Until a recorder exists, the VOD is the only copy of a detected live.
- WebSub push needs a public HTTPS callback, which the tailnet-only cluster lacks. Not used.

### Recording and the VOD

- An upcoming stream gets a recorder Job a few minutes before `scheduled_at`, running with `--wait-for-video` and `--live-from-start`. Not built.
- A live recording can have gaps or buffering, so when both a recording and the VOD exist, the two are compared. How to compare and which to keep is open.
- Some streams get no VOD after they end. For those, the recording is the only copy and stays.

### Login

- The backend is the OpenID Connect client against Zitadel at `https://auth.hiriri.com`: authorization code with PKCE (S256). The browser holds only a signed session cookie with `sub`, name, email and `groups`. No token reaches the frontend.
- A client secret is optional. Unset means a public client, a Zitadel Web application with authentication method `None`.
- Zitadel project `savetube`, application `savetube`, role `savetube-admin`. A project of its own, because savetube writes `tank/youtube` and an `internal` grant must not pass its login (homelab-platform `docs/zitadel-oidc-setup.md`).
- `SAVETUBE_OIDC_REQUIRED_GROUP` checks the flat `groups` claim the `groupsClaim` Action writes, read from the userinfo endpoint. A user without the group gets 403 and no session.
- Redirect URI `https://savetube.lab.hiriri.com/auth/callback`. Post Logout URI `https://savetube.lab.hiriri.com/`. Logout is a POST that clears the session and sends the browser to `end_session` with `client_id`.
- Redirect URIs and the cookie's Secure flag come from `SAVETUBE_PUBLIC_URL`. Uvicorn runs with proxy headers off, so no forwarded header is trusted.
- `/api/*` answers 401 without a session. `/healthz`, the static bundle, and the metrics port need none.

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

## Unsettled

- Whether `statfs` through virtiofs reports the dataset's free space or the pool's.
- Which Google account holds the memberships. Using the main account puts it at ban risk. A separate account needs its own memberships.
- Whether members-only uploads and lives appear in RSS and `videos.list`. If not, the `/live` poll and the members playlist need cookies. The `UUMO` playlist-id prefix for members-only uploads is third-party lore, not checked.
- Whether issue 15274's fix (PR 16771) is in a release.
- A read-only Samba mount of `tank/youtube` for watching on a PC. Samba stays a reader, so the dataset keeps one writer.
- Live chat capture during a live, not only replay. Needs a separate library. Not surveyed.
- Offsite backup: none of the current tiers can carry this size.

## Build order

1. Prove an image with Python, Deno, ffmpeg, yt-dlp and the bgutil plugin runs one from-start capture of a public live in the cluster through `YoutubeDL`.
2. Prove a members-only download and a members-only live with the chosen account's cookies.
3. Build the recorder Job and the scheduler's live detection, with Discord notifications. Live recording is the requirement nothing else covers.
4. Build the archive worker, the queue, and the tree layout.
5. Build the UI from `dashboard-01`: statistics, sources and rules, jobs, the video browser.
