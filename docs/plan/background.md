# Background

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

One custom Python application embeds yt-dlp as a library. Channels, sources, jobs, and metadata live in one Postgres database, and one UI covers archives, lives, and statistics.

Requirement 3 needs a custom recorder, database, and UI under any option, so the archive downloader on top costs one queue and one yt-dlp call.

Rejected:

- Pinchflat-NGX for archives beside a custom recorder: two UIs, two rule sets, dedupe between them, and a one-maintainer fork.
- Pinchflat-NGX with avtdl driving yt-dlp: three UIs, and recorded lives appear in no browse UI.
