# Storage

## Data model

| Table | Holds |
| --- | --- |
| `channel` | channel id, handle, display name, avatar URL, the directory name fixed at first add, include and exclude title words, date cutoff, format selector, watch lives |
| `cookie_profile` | the cookie profile names the scheduler last found in `SAVETUBE_COOKIES_DIR`, each with the time it was seen, the YouTube account name and handle its file signs in, the file's sha256 the account was read from, and the read time |
| `membership` | per channel and cookie profile: whether the profile's account is a member, when the check ran, the members-only video it read |
| `global_words` | one row: the global include and exclude title words |
| `source` | a channel tab, a channel's playlists, or a playlist URL, enabled flag, archiving flag, poll interval, include and exclude title words, other-channel flag, parent source, last poll time and error |
| `listing_entry` | every video a source's last listings showed: source, video id, channel, title. Title word suggestions read it |
| `video` | video id, channel, type, title, upload date, duration, live status, availability, format ids, file paths, `size_bytes`, `completed_at`, state, the pinned format of a partial download, the integrity checks it passed and when, and the stored streams' quality generated from `info` |
| `video_source` | which sources reference a video, so a playlist never copies a file |
| `job` | kind (archive, record, replace), state, attempts, miss reason, log tail, the running download's progress, the format ids a replace downloads, `manual` for a replace the user picked, timestamps |
| `format_watch` | per stored live, or per video a user's probe listed: the live's air time as `started_at`, null for a row without an automatic watch, when the watch ended, the last probe's time, counted formats, VOD duration and error, the stored file's duration, the manual-pick flag, the path of the original moved aside while it is kept, and `original_until`, when an original kept outside an open watch is deleted |
| `live_seen` | per video id live detection probed: the probe's fields as JSONB, null for a probe that failed, `probed_at`, the last probe, and `listed_at`, the last check that listed the id (live.md, Live detection) |
| `storage_sample` | hourly free and used bytes of the dataset |
| `encode_settings` | one row: re-encoding on or off and its profile (encode.md) |
| `encode_job` | per job file under `.encode/`: video, state (queued, claimed, succeeded, failed, cancelled, requeued, lost), profile, the stored file's path and size at queueing, the encoder, ffmpeg version and result size from the job file, the failure reason and log tail, when the scheduler last saw the file |
| `video.info` | the full yt-dlp info dict as JSONB |

- `channel.avatar_url` is the smallest square thumbnail of a tab listing, else its `avatar_uncropped` original. The scan finds it for a new channel, and every tab poll refreshes it. A playlist listing carries the playlist's thumbnails, so a playlist poll leaves it alone.
- `video.video_codec`, `audio_codec`, `video_width`, `video_height`, `video_fps`, `video_kbps` and `premium` are stored generated columns over `info`. They read the stored streams the way `savetube.formats.stored_streams` does: the first requested format with that stream, else the info dict itself. A replace keeps the top-level `format_note` of the first download, so `premium` reads the video stream's note. `video_kbps` is `vbr`, else `tbr`. The Videos page filters on them, because reading the TOASTed `info` took about 2 s per query on 5,000 videos and the columns take about 1 ms.
- Migration `0026` adds `encode_settings`, with its row off and profile `vtuber-av1-cq46`, and `encode_job`, empty. Its downgrade drops both. Files under `.encode/` stay.
- Migration `0025` adds `live_seen`, empty, so the first check after it probes every candidate id once more. Its downgrade drops it.
- Migration `0024` drops `download_settings.move_requested_at`, the Move now flag before the Scheduler API (runtime.md). Its downgrade adds it back, null.
- Migration `0023` makes `format_watch.started_at` nullable and adds `format_watch.original_until` and `job.manual`. It clears `original_path` of every ended watch, whose end deleted the original, and marks the queued or running replace of a picked watch `manual`. Its downgrade deletes the rows without `started_at`.
- Migration `0022` adds those generated columns. It rewrites the `video` table under an exclusive lock, about 40 s on 5,000 videos with 150 KB info dicts. Its downgrade drops them.
- Migration `0021` sets each watch's `started_at` to its live's air time (live.md, Format watch). It cancels every queued replace job of an old watch, one whose live aired 7 or more days ago or has no air time, and every queued replace job of a recent watch without a manual pick, so the next probe queues again what still ranks above. A queued job whose procrastinate job a worker already took stays. It ends every open old watch, unless a replace job is still queued or running or an original sits in `.originals/`. The scheduler ends the latter and deletes the original, so the migration deletes no file. Its downgrade does nothing.
- Migration `0020` adds `format_watch`, `job.format_id` and the job kind `replace`. It starts a watch for every `complete` live video whose `completed_at` is within 7 days, from its `completed_at`. Its downgrade deletes every replace job and drops both. Files a replace moved aside stay in `.originals/`.
- Migration `0018` adds `cookie_profile.account_name`, `account_handle`, `account_digest` and `account_checked_at`, all null. Its downgrade drops them.
- Migration `0014` adds the video state `staged` (Staging). Its downgrade turns every `staged` video into `pending`, so the old code downloads it again.
- Migration `0013` adds `download_settings.window_enabled`, true where both window times are set, and `download_settings.daily_limit`, null. Its downgrade drops both.
- Migration `0012` sets every `failed` video with a queued archive job back to `pending`. The poll used to queue a retry without resetting the video. Its downgrade changes nothing.
- Migration `0011` adds `download_settings.paused`, false on upgrade. Its downgrade drops it.
- Migration `0010` adds `cookie_profile`, `membership` and `download_settings.cookie_profile`, and drops `channel.use_cookies`. Its downgrade adds `use_cookies` back as false on every channel.
- Migration `0008` adds the job state `interrupted` and `video.resume_format`, `verified_at` and `verified_checks`. Its downgrade turns every `interrupted` job into `cancelled`, because the old code counts a failed job as an attempt.
- Migration `0007` adds `channel.avatar_url` and `job.progress`. An existing channel gets its avatar on the next poll of one of its tab sources.
- `job.progress` holds the current stream of a running archive download: format, percent, downloaded and total bytes, speed and ETA. The download child sends it through a pipe at most once a second. The worker process writes the latest value to the row at most every 5 s, so the child opens no database connection. After the last byte, it holds `{step}` instead: `merging` when yt-dlp's Merger starts, `verifying` before the integrity checks, and `audio` before the audio copy.
- The scheduler's hourly database cleanup clears `job.progress` on every job that is not `running`, deletes finished procrastinate jobs and their events after 30 days, deletes the `interrupted` archive rows of a video 30 days after its latest succeeded archive job (archive.md, Resuming and verifying downloads), and deletes each `live_seen` row 30 days after its `listed_at`.
- Migration `0005` moves the settings onto `channel` from the rules of the channel's sources, then drops `source.rule_id` and the `rule` table. Where those rules differ, `watch_lives` and `use_cookies` are on when any was on, `date_after` takes the earliest, and `format` comes from the lowest source id.
- The `.info.json` beside each video is the source of truth. A rescan of the tree rebuilds `video` without network calls.
- A playlist is a relation, not a directory. A video sits once under its uploader's channel, and the UI shows the playlist view.

## Layout

New dataset `tank/youtube`, mounted read-write in Talos. Inside it:

```
<channel name> [<channel id>]/
  videos/  <YYYY-MM-DD> <title> [<video id>]/
  shorts/  <YYYY-MM-DD> <title> [<video id>]/
  lives/   <YYYY-MM-DD> <title> [<video id>]/
  members/ <YYYY-MM-DD> <title> [<video id>]/
.incoming/<video id>/   archive downloads without staging
.incoming/<job id>/     recordings, and replace jobs' remux output
.originals/<video id>/  the file a replace or a re-encode moved aside, until the open watch ends, or 7 days after a swap outside one
.encode/                job files and results of the external encoder (encode.md), also a Samba share
```

- Each video directory holds the media, its audio-only copy (archive.md, Audio copy), `.info.json`, the thumbnail, subtitles, and the chat replay when YouTube offers one.
- Type rule, first match wins: `availability == subscriber_only` is `members`. A `was_live` or recorded live is `lives`. A video from the `/shorts` tab is `shorts`. The rest is `videos`.
- The channel directory name is fixed at first add and stored in `channel`. A later channel rename changes the display name only, never the path.
- Downloads land in `.incoming/` on the same dataset, so the final move is a rename and a half-written file never appears in the tree. With staging, archive downloads land on the staging volume instead (Staging). An archive download's directory is per video, so a retry resumes its parts (archive.md, Resuming and verifying downloads).
- Titles pass through yt-dlp's filename sanitising and a length cap, because a title can exceed the 255-byte name limit in UTF-8.
- Format: `bv*+ba/b`, merged to `mkv`, which holds any codec pair YouTube serves.
- An archive download and a replace sort formats by resolution, then fps, then HDR over SDR, then AV1 over VP9 over H.264 (`format_sort` `["res", "fps", "hdr:12", "vcodec:av01"]`, `FORMAT_SORT` in `savetube.ytdl`). These fields go before the YouTube extractor's own, whose `source` field ranks Premium VP9 (`616`) above AV1. Audio-only formats tie on all four, so Opus `251` stays the audio pick. A replace names its format ids, so the sort changes nothing it downloads. A recording keeps yt-dlp's order.
- Files inside a video directory are named `<video id>.<ext>`, so only directory names carry a title and need the byte cap.
- yt-dlp's `.tmp` fragments directory is deleted before the rename. The cookie copy lives in a private temporary directory outside `.incoming/`, so it never moves into the tree.

## Staging

The archive's disks are loud while a download writes to them for hours. With `SAVETUBE_STAGING_ROOT` set, archive downloads use a separate volume on quiet disks, and finished ones move into the tree in batches.

```
<staging>/incoming/<video id>/   archive downloads
<staging>/ready/<video id>/      finished, waiting for the move
```

- The download, the integrity checks and the audio copy all run on staging. A passed download is renamed into `ready/` and its video becomes `staged`. Its job succeeds, and `dir_path` already names its place in the tree.
- A `staged` video is hidden like any video that is not `complete`: no list shows it and nothing plays it. The poll counts it as stored and queues no job for it.
- The scheduler checks every 60 s whether the staged videos move (`savetube.staging`), and at once on Move now. They move when at least one of these holds:
  - The staged videos hold 80 GiB or more (`MOVE_BATCH_BYTES`), even while a download runs. On a volume smaller than about 95 GiB, the free-space rule fires first.
  - Staging has less than 15 GiB free (`MIN_FREE_BYTES`). The largest stored video is 7 GB, and yt-dlp holds about twice that while it merges.
  - No archive download runs, and the newest staged video has waited 1 hour (`MOVE_IDLE`).
  - The user pressed Move now on the Downloads card. `POST /api/staging/move` calls the scheduler's `POST /staging/move` (runtime.md, Scheduler API), because the web pod mounts neither staging nor a writable archive. A request made during a move runs one more check after it, because it may name a video staged during the move.
- A move copies every staged video, oldest first. Each copy goes to `.incoming/<video id>.moving/` on the archive, is renamed to its place in the tree, and its `ready/` directory is deleted. Then the video becomes `complete`.
- A staged video whose place in the tree exists was renamed there by a move cut off before its row changed. It becomes `complete`. To keep that rule true, a download is not staged when its place in the tree already exists.
- A staged video whose files are in neither place goes back to `pending`, and the next poll downloads it. It logs `staging.missing` at WARNING.
- A missing staging root moves nothing and logs `staging.unmounted` at ERROR, so a lost mount never reads every staged video as gone.
- Recordings do not use staging.
- A replace job (live.md, Format watch) downloads its stream into `<staging>/incoming/<job id>/` and writes the remux into the archive's `.incoming/<job id>/`, because the result renames over a file in the tree. It never waits for a batch move, and its video stays `complete` throughout.
- In production, staging is the 100Gi PVC `savetube-staging` of StorageClass `nvme`, on the `rpool` NVMe mirror. The worker and the scheduler mount it at `/staging`. A new volume's root belongs to root, so an init container running as root with only `CHOWN` gives it to uid 1000. A pod-level `fsGroup` would also chown the archive's local PV recursively.
- Switching staging on leaves the parts already in the archive's `.incoming/`. Their videos download again from zero on staging, and the hourly cleanup deletes the old parts after 7 days.
