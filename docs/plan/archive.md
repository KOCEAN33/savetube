# Archive

## Source polling

- A source is one channel tab (`videos`, `shorts`, `streams`), one channel's playlists (`playlists`), or one playlist, stored as a canonical URL. Adding a bare channel URL creates one source per chosen tab.
- The scheduler lists a due source with yt-dlp's flat extraction: ids and titles, no request per video. Listing a channel with a few thousand videos takes minutes, because every page request sleeps 0.75 s.
- A tab's entries carry no `channel_id`, so the poll takes it from the listing. Playlist entries carry their own, and a private or deleted entry without one is skipped.
- Every listed video that has a `video` row gets a `video_source` link, whether or not it passes the filters. A video that passes and is not yet stored gets a `pending` `video` row and an archive job, unless a job for it is queued or running, or three archive jobs for it have failed.
- A poll puts no job on the procrastinate queue. A new archive job waits as `queued` until the scheduler's dispatcher queues it (Download limits).
- A members-only entry, flat `availability` `subscriber_only`, does not pass when the source's channel has `use_cookies` off, because every download attempt would fail. It keeps its `listing_entry` row and its `video_source` link. The cancel rule for skipped titles (Title words) cancels its queued archive job. The first poll after `use_cookies` is turned on queues it.
- `is_live`, `is_upcoming` and `post_live` entries are left to live detection.
- The channel's date cutoff reads the entry's approximate timestamp ("3 weeks ago"), so it is only as exact as YouTube's relative date. A video uploaded a day before the cutoff can pass.
- A failed archive job deletes its `.incoming/<video id>/`, because the retry after a real failure starts over (Resuming and verifying downloads).
- Every write to `/api/*` needs an `X-Requested-With` header. A cross-origin page cannot send it without a CORS preflight, and SameSite=lax still sends the cookie on a POST from a sibling `hiriri.com` subdomain.

## Channel playlists

- Two modes. "Track all" is one source of kind `playlists` at `https://www.youtube.com/<channel>/playlists`. "Pick some" is one `playlist` source per chosen playlist.
- A `playlists` source downloads nothing itself. Each poll lists the channel's playlists and creates a child `playlist` source for every playlist URL that is not a source yet. A child stores `parent_id` and starts with `last_polled_at` set to a random point within its interval, so new children do not all fall due at once. Deleting the parent deletes its children.
- A child copies the parent's poll interval and other-channel flag at creation. A later change of those on the parent is copied to every child. A child is skipped while its parent is disabled.
- A child cannot be deleted, only disabled. Its row stays, so the parent poll never creates it again and its title stays visible. `DELETE` on a child returns 409. Enabling it again restores it.
- A child whose playlist no longer appears in a successful, non-empty parent listing is disabled with `last_error` "playlist no longer listed on the channel".
- Each playlist stays its own source, so `video_source` keeps playlist membership for the playlist view.
- `source.title` holds the playlist's title for a `playlist` source. The parent poll sets it on create and refreshes it on every poll, and a playlist's own poll refreshes it from its listing. `GET /api/sources` carries `title`, null for a tab.
- `source.other_channels`, default off, decides whether a playlist source archives videos uploaded by other channels. Off keeps only entries whose `channel_id` equals the source's channel. A child always keeps its parent's channel. A playlist source without a parent takes its channel from the listing's `channel_id`. With `other_channels` off and no channel, the poll queues nothing and sets `last_error`.
- `POST /api/channels` sets `other_channels` on its playlist and `playlists` sources, on by default. Its tab sources always keep it off. `POST /api/sources` defaults it to off.
- A video another channel uploaded is stored once, under its uploader's channel directory. `store()` creates the uploader's `channel` row and directory on its first download. The source's channel page lists the video through its `video_source` link.

## Title words

- Word lists exist at three levels: global, channel, and source. Each holds `include_words` and `exclude_words`.
- An entry's effective include list is the union of the global list, the list of the entry's uploader channel, the parent source's list for a child, and the source's own list. The effective exclude list is the union of the same exclude lists.
- A title passes when the effective include list is empty or any word in it is in the title, and no effective exclude word is in the title. Matching is a case-insensitive substring test on casefolded text.
- Example: global include `歌枠` keeps `歌枠` titles on every channel. A channel that also wants covers adds `Cover` to its own include list. A source with no words at any level keeps every title.
- The archive poll and live detection both apply the effective lists. A detected live queues a `record` job only when its title passes the effective lists of at least one watching source: an enabled tab source of a channel with watch lives on. When such a source exists on the `streams` tab, only `streams` sources count.
- Live detection caches each probe's result per process and applies the words on every tick, so a word change takes effect without a restart.
- A poll cancels a queued archive job, and its procrastinate job, for a listed video that no longer passes. A running job continues.
- A list holds at most 200 words of up to 100 characters each.
- The global lists live in the single-row table `global_words`. The channel lists are columns on `channel`.
- The poll replaces the source's `listing_entry` rows with the full listing before any filtering, so suggestions and the preview see titles the words skip.
- A suggestion is a word that recurs in one channel's titles. The extractor takes the text inside `【】`, `[]`, `()`, `（）`, `「」` and each `#hashtag`. Only the outermost bracket counts, so `【ゲリラ(豪雨)】` gives `ゲリラ(豪雨)`. A word is casefolded with a leading `#` stripped, so `Cover` and `#cover` are one word, shown in its most frequent original form without `#`.
- Ranking counts distinct videos of the channel that carry the word. A word on one video only is dropped. The top 30 are returned.

## API for playlists and title words

| Method and path | Body or query | Returns |
| --- | --- | --- |
| `POST /api/sources` | `tabs` also accepts `playlists`. New fields `include_words`, `exclude_words` (lists of strings, default empty) and `other_channels` (default false) | the created sources |
| `PATCH /api/sources/{id}` | also `include_words`, `exclude_words`, `other_channels`, `enabled` | the source |
| `GET /api/sources` | | each source also carries `include_words`, `exclude_words`, `other_channels`, `parent_id`, `title` |
| `POST /api/channel-playlists` | `{url}`, a channel URL | `[{id, title, url, source_id}]`, `source_id` null for a playlist that is not a source yet. Lists live through yt-dlp, at most two at a time, 502 on a yt-dlp error, 504 after 120 s |
| `GET /api/channels/{id}/words` | | `[{word, count}]`, best first |
| `POST /api/sources/{id}/title-preview` | `{include_words, exclude_words}`, the source's own lists being edited | `{total, kept, titles: [{video_id, title, kept}]}` from the source's `listing_entry` rows, matched with the stored global and channel lists merged in |
| `GET /api/words` | | the global `{include_words, exclude_words}` |
| `PUT /api/words` | `{include_words, exclude_words}` | the global lists |
| `GET /api/channels` | | every stored channel, uploader-only ones included. Each also carries `include_words`, `exclude_words`, `date_after`, `format`, `use_cookies`, `watch_lives`, and `tracked`, true when at least one source names the channel |
| `PATCH /api/channels/{id}` | `{include_words?, exclude_words?, date_after?, format?, use_cookies?, watch_lives?}` | the channel |

- Word lists are trimmed, empty strings dropped, and duplicates removed after casefolding, keeping the first form.

## Pausing downloads

- A source with `archiving` off is still polled. Its polls write `listing_entry` rows and `video_source` links but create no `video` rows and no archive jobs. The channel page turns it off to pause a channel's downloads.
- The words preview at onboarding reads the first scan, so a channel added through `POST /api/channels` starts with `archiving` on.
- `PATCH /api/sources/{id}` with `archiving: true` resumes downloads, and the source is polled on the next scheduler tick. On a `playlists` parent the flag is copied to every child, and a child created later takes the parent's value.
- Sources that existed before migration 0003 keep downloading: the migration sets `archiving` on for them.
- Live detection ignores `archiving`.

## Download limits

- One global row, `download_settings`, holds three limits. They apply to archive jobs only. A live recording ignores all three, because a live missed is lost.
- `max_concurrent`, 1 to 10, default 1: archive downloads running at once. The chart's `worker.concurrency` is the hard ceiling and defaults to 10, so the setting decides.
- `rate_limit_bytes`, null for no limit: passed to yt-dlp as `ratelimit`, so it limits each download. Three downloads at once can use three times the rate.
- `window_start` and `window_end`, `HH:MM` in Asia/Seoul, both null for no window: archive downloads start only inside the window. A window may cross midnight, such as 23:00-05:00. Equal start and end is rejected.
- A download already running when the window closes runs to the end.
- A waiting archive job is a `queued` `job` row with no procrastinate job (`queue_job_id` null). Waiting adds no row to `procrastinate_jobs`.
- The scheduler dispatches every 10 s, apart from its 60 s tick, because with `max_concurrent` 1 a slot sits idle until the next dispatch. Inside the window, the dispatcher queues the oldest waiting jobs, by job id, up to `max_concurrent` minus running archive jobs minus dispatched jobs. A dispatched job is a `queued` row with a procrastinate job. Outside the window it queues nothing.
- `job.deferred_until` holds the next window start for a waiting job outside the window, and null inside it. The dispatcher sets it on every run.
- A dispatched job whose procrastinate job is no longer `todo` or `doing`, such as one that failed before the worker marked the row, goes back to waiting on the next dispatch. Otherwise it would hold its slot for good.
- The worker checks the window and the free slots again before it starts. If it may not start, it hands the job back to waiting and queues nothing. The check, the `running` mark, and the dispatch all take one Postgres advisory lock, so a slot is never taken twice.
- The worker deletes a procrastinate job when it succeeds, so `procrastinate_jobs` holds only todo, running, failed and cancelled jobs. A `job` row keeps the history.
- A `PUT /api/download-settings` changes no job. The next dispatch, within 10 s, applies the new limits.
- Migration `0006` deletes every `succeeded` procrastinate job and every `todo` archive procrastinate job. It clears `queue_job_id` and `deferred_until` on every `queued` archive job whose procrastinate job is not `doing`, so the dispatcher takes it. A `doing` job and its `job` row stay. The downgrade cancels every waiting archive job, so the old poll queues its video again.
- Process health and job ownership are separate. Kubernetes judges health: worker and scheduler serve `GET /healthz` on `SAVETUBE_HEALTH_PORT` (default 8081) from their own asyncio loop, so a stuck loop fails the probe, and the chart sets a livenessProbe on it. The metrics port cannot serve this, because `prometheus_client` answers from its own thread while the loop hangs. Docker compose runs the same check as a `healthcheck`. Job ownership stays in Postgres as procrastinate's heartbeat lease.
- A YouTube block pauses archive downloads. A block is a `DownloadFailed` whose text, lowercased, holds both "confirm you" and "not a bot", or "http error 429". The bot check text has a straight or a curly apostrophe, so no marker includes it. `savetube.jobs.BLOCK_MARKERS` holds the markers.
- A blocked archive job is interrupted, not failed: it counts as no attempt, keeps `.incoming/<video id>/` and its pinned format, and logs `job.interrupted` at WARNING with reason "blocked by YouTube". The `interrupted` row's `log_tail` holds the yt-dlp text.
- The first block past an expired pause adds 1 to `download_settings.block_count` and sets `blocked_until` to now plus the backoff, under the slot lock. The backoff is 30 min, doubling with each count, at most 12 h: 30 min, 1 h, 2 h, 4 h, 8 h, 12 h. It logs `dispatch.blocked` at WARNING with `until`, `backoff`, `count`, the job, the video, and the reason.
- A block while a pause holds, such as jobs refused together, only interrupts its job.
- While `blocked_until` is in the future, the dispatcher queues nothing and the worker hands a job back with `job.returned` reason "blocked". `job.deferred_until` holds the later of `blocked_until` and the next window start.
- After the pause and until a download succeeds, `block_count` is above 0 and the effective `max_concurrent` is 1. One job tests whether YouTube still blocks.
- A succeeded archive job after the pause resets `block_count` to 0 and `blocked_until` to null, and logs `dispatch.unblocked` at INFO with the previous count. A success while a pause holds resets nothing, because that download started before the block.
- A live recording ignores blocks. A blocked recording fails like any other failure.
- Migration `0009` adds `blocked_until` and `block_count` and turns every `failed` archive job whose `log_tail` holds "not a bot" into `interrupted`, so a bot-check wave gives back the attempts it took. The downgrade drops the columns and keeps those jobs `interrupted`.
- The scheduler reclaims an archive job left `running` by a dead worker every minute. It reads procrastinate's stalled jobs, found by the worker heartbeat. The job is interrupted with `log_tail` "worker stopped during the download" and logs `job.interrupted` at WARNING. Without this, a dead worker's jobs hold download slots forever.

| Method and path | Body | Returns |
| --- | --- | --- |
| `GET /api/download-settings` | | `{max_concurrent, rate_limit_bytes, window_start, window_end, timezone, blocked_until, block_count}`. The last two are read-only |
| `PUT /api/download-settings` | `{max_concurrent, rate_limit_bytes, window_start, window_end}` | the settings |
| `GET /api/jobs` | | each job also carries `deferred_until` |
| `PATCH /api/sources/{id}` | also `archiving` | the source, with `archiving` |

## Resuming and verifying downloads

- An archive download works in `.incoming/<video id>/`, so every job for one video uses the same directory. A recording keeps `.incoming/<job id>/` and starts from the first segment on every attempt.
- An interrupted run is one the worker stopped: a worker shutdown, or a reclaim after the worker died. It counts as no attempt. Only `failed` jobs count toward `MAX_ARCHIVE_ATTEMPTS`.
- An interruption keeps the job's id, so the job keeps its place in the queue. The run that started becomes its own `job` row with state `interrupted`, its start and end time, and the reason in `log_tail`. The job goes back to `queued` with no procrastinate job and `attempts` back where it was, so the dispatcher queues it again within 10 s. A dispatched job that no worker started goes back to waiting with no `interrupted` row.
- An interruption keeps `.incoming/<video id>/`. The video goes back to `pending`.
- On SIGTERM the worker stops taking jobs and cancels its downloads at once (`shutdown_graceful_timeout=0`). A cancel kills the download child, whose `.part` files stay, marks the job interrupted, and logs `job.interrupted` at INFO. The child ignores SIGINT and SIGTERM, so only the parent ends a download.
- The worker's `terminationGracePeriodSeconds` is 30. A shutdown takes a few seconds, one kill and one database write per download. A worker still running at the deadline is killed, and the reclaim interrupts its jobs once procrastinate sees its heartbeat stop.
- `download.format` stores the chosen format ids, such as `137+140`, in `video.resume_format`. The child sends them to the parent through the progress pipe.
- A retry with a pinned format and an existing directory passes the pinned format ids to yt-dlp instead of the channel's selector, with `continuedl` on, so yt-dlp continues each `.part` file. It logs `download.resume` with the bytes in the directory and in `.part` files.
- When yt-dlp answers "Requested format is not available" to the pinned format, the run logs `download.restart` at WARNING, deletes the directory and the pin, and downloads again with the channel's selector in the same attempt.
- A pin without its directory is cleared, and the download starts with the channel's selector.
- A succeeded download moves the directory into the tree and clears the pin. A failed one deletes the directory and clears the pin.
- The scheduler deletes, every hour, a directory under `.incoming/` with no file written for 7 days whose name is neither the video of a queued or running archive job nor the id of a queued or running job. It logs `incoming.deleted`. Seven days is longer than any wait for a download window, and a waiting job's directory is never deleted. Cancelled jobs and a video whose source stops archiving leave such directories.
- Before the move into the tree, `savetube.integrity` checks the download:
  - size: each stream file before the merge has the size of its format's exact `filesize`. A format with only `filesize_approx` is not checked. The child reports each finished stream's size, because the merge deletes the stream files.
  - probe: `ffprobe` reads the final file. Its duration is within the larger of 2 s and 1% of the info dict's `duration`, when the info dict has one. It has a video stream when the format's `vcodec` is not `none`, and an audio stream when its `acodec` is not `none`.
  - packets: a resumed download also gets `ffmpeg -v error -xerror -i <file> -c copy -f null -`. A stream copy decodes nothing, so a multi-hour file takes about as long as reading it. Any error output fails the check.
- Without `ffprobe` or `ffmpeg` on the path, probe and packets are skipped with a DEBUG line. The image carries both.
- A failed check logs `download.integrity_failed` at WARNING with the check and its numbers. The run deletes the directory and the pin and downloads from zero once, in the same attempt. A second failure fails the job with `log_tail` "integrity check failed: <check> ...", which counts as an attempt.
- A passed download logs `download.verified` with the checks that ran. `video.verified_at` and `video.verified_checks` hold the result, and `GET /api/videos` and `GET /api/videos/{id}` carry both.
- The channel's Downloads list shows `interrupted` rows among the finished jobs.
