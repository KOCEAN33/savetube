# Archive

## Source polling

- A source is one channel tab (`videos`, `shorts`, `streams`), one channel's playlists (`playlists`), or one playlist, stored as a canonical URL. Adding a bare channel URL creates one source per chosen tab.
- The scheduler lists a due source with yt-dlp's flat extraction: ids and titles, no request per video. Listing a channel with a few thousand videos takes minutes, because every page request sleeps 0.75 s.
- A tab's entries carry no `channel_id`, so the poll takes it from the listing. Playlist entries carry their own, and a private or deleted entry without one is skipped.
- Every listed video that has a `video` row gets a `video_source` link, whether or not it passes the filters. A video that passes and is not yet stored gets a `pending` `video` row and an archive job, unless a job for it is queued or running, or three archive jobs for it have failed. A video queued again after a failure goes back to `pending`.
- A poll puts no job on the procrastinate queue. A new archive job waits as `queued` until the scheduler's dispatcher queues it (Download limits).
- A poll lists with the general cookie profile, or cookie-free when none is set (access.md, Cookies and account).
- A members-only entry, flat `availability` `subscriber_only`, passes only when a present cookie profile is a confirmed member of the entry's channel. Otherwise every download attempt would fail. A skipped entry keeps its `listing_entry` row and its `video_source` link. The cancel rule for skipped titles (Title words) cancels its queued archive job. The first poll after a membership check confirms a member queues it.
- `channel.members_all`, default off, exempts the channel's members-only entries from the title words. The uploader channel's switch applies, the same channel whose word list applies. The membership check, the date cutoff, the other-channel check and the live check still apply to them.
- After a poll that lists members-only entries, the membership checks of their channels run (access.md, Cookies and account).
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
- A members-only entry of a channel with `members_all` on passes the words whatever its title.
- `channel.playlists_pass`, default off, drops the include list for the channel's `playlist` sources, children of a `playlists` source included. A title then passes unless an effective exclude word is in it. The source's channel decides, not the uploader, because the playlist belongs to the source's channel. Tab sources still apply the include list.
- `channel.keep_all`, default off, drops every title word for every source of the channel: tab sources, `playlist` sources, and children of a `playlists` source. Global, channel, parent, and source lists, include and exclude, all drop. The source's channel decides, as for `playlists_pass`. The unfinished live skip, the member profile check, `other_channels`, and `date_after` still apply. A global include word then cannot shrink a channel the user backs up whole.
- The archive poll and live detection both apply the effective lists. A detected live queues a `record` job only when its title passes the effective lists of at least one watching source: an enabled tab source of a channel with watch lives on. When such a source exists on the `streams` tab, only `streams` sources count.
- Live detection caches each probe's result per process and applies the words on every tick, so a word change takes effect without a restart.
- A poll cancels a queued archive job, and its procrastinate job, for a listed video that no longer passes. A running job continues.
- The job stays when another source that lists the video still passes it: an enabled source, with its parent enabled, with `archiving` on, judged by its own filters on this listing's entry. `members_all`, `playlists_pass`, and `keep_all` count in that judgement, so a members-only video or a playlist entry it keeps is not cancelled for words. A lost membership still cancels it.
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
| `POST /api/sources/{id}/title-preview` | `{include_words, exclude_words}`, the source's own lists being edited | `{total, kept, titles: [{video_id, title, kept}]}` from the source's `listing_entry` rows, matched with the stored global and channel lists merged in. A `playlist` source of a channel with `playlists_pass` on matches with the exclude list only. Every title is kept for a source of a channel with `keep_all` on |
| `GET /api/words` | | the global `{include_words, exclude_words}` |
| `PUT /api/words` | `{include_words, exclude_words}` | the global lists |
| `GET /api/channels` | | every stored channel, uploader-only ones included. Each also carries `include_words`, `exclude_words`, `date_after`, `format`, `watch_lives`, `members_all`, `playlists_pass`, `keep_all`, `tracked`, true when at least one source names the channel, and `memberships: [{profile, is_member, checked_at}]`, by profile name |
| `PATCH /api/channels/{id}` | `{include_words?, exclude_words?, date_after?, format?, watch_lives?, members_all?, playlists_pass?, keep_all?}` | the channel |

- Word lists are trimmed, empty strings dropped, and duplicates removed after casefolding, keeping the first form.

## Pausing downloads

- A source with `archiving` off is still polled. Its polls write `listing_entry` rows and `video_source` links but create no `video` rows and no archive jobs. The channel page turns it off to pause a channel's downloads.
- The words preview at onboarding reads the first scan, so a channel added through `POST /api/channels` starts with `archiving` on.
- `PATCH /api/sources/{id}` with `archiving: true` resumes downloads, and the source is polled on the next scheduler tick. On a `playlists` parent the flag is copied to every child, and a child created later takes the parent's value.
- Sources that existed before migration 0003 keep downloading: the migration sets `archiving` on for them.
- Live detection ignores `archiving`.
- `download_settings.paused` pauses every archive download. The user sets it with the header's pause switch (frontend.md). Polls go on and still create archive jobs, which wait.
- While paused, the dispatcher queues nothing and the worker hands a job back with `job.returned` reason "paused". `job.deferred_until` stays null, because a pause has no known end.
- A pause also stops running archive downloads. Each running download reads the flag every 5 s (`PAUSE_POLL_SECONDS` in `savetube.jobs`), so a download stops within about 5 s. The stop is a SIGTERM cancel (Resuming and verifying downloads): the child is killed, the `.part` files and the pin stay, the job is interrupted with no attempt used and `log_tail` "paused by user", and `job.interrupted` logs at INFO. The queue job ends as succeeded.
- A failed flag read logs `job.pause_check_failed` at WARNING, and the download goes on.
- Resuming clears the flag. The next dispatch, within 10 s, queues waiting jobs, and an interrupted job resumes its parts.
- `PUT /api/download-settings/paused` logs `dispatch.paused` or `dispatch.resumed` at INFO with the running archive downloads, only when the value changes. `PUT /api/download-settings` leaves the flag as it is, so saving the limits never resumes downloads.
- A live recording ignores the pause and is never stopped by it.

## Download limits

- One global row, `download_settings`, holds four limits. They apply to archive jobs only. A live recording ignores all four, because a live missed is lost.
- `max_concurrent`, 1 to 10, default 1: archive downloads running at once. The chart's `worker.concurrency` is the hard ceiling and defaults to 10, so the setting decides.
- `rate_limit_bytes`, null for no limit: passed to yt-dlp as `ratelimit`, so it limits each download. Three downloads at once can use three times the rate.
- `window_start` and `window_end`, `HH:MM` in Asia/Seoul, both set or both null: archive downloads start only inside the window. A window may cross midnight, such as 23:00-05:00. Equal start and end is rejected.
- `window_enabled` switches the window on and off. The window applies only when `window_enabled` is true and both times are set. Turning it off keeps the stored times. Enabling it with a missing time is a 422.
- Migration `0013` sets `window_enabled` true where both times are set, else false.
- A download already running when the window closes runs to the end.
- `daily_limit`, at least 1, null for no cap: archive downloads per Asia/Seoul calendar day, which resets at 00:00 KST. The count is the archive jobs `succeeded` with `finished_at` since today's 00:00 KST, plus the running archive jobs. A job starts only while the count is below `daily_limit`.
- The count holds successes, not starts, so an interrupted or resumed run takes nothing from the cap. A failed run takes nothing either.
- A download already running when the count reaches `daily_limit` runs to the end.
- A waiting archive job is a `queued` `job` row with no procrastinate job (`queue_job_id` null). Waiting adds no row to `procrastinate_jobs`.
- The scheduler dispatches every 10 s, apart from its 60 s tick, because with `max_concurrent` 1 a slot sits idle until the next dispatch. Inside the window and under the daily cap, the dispatcher queues the oldest waiting jobs, by job id, up to `max_concurrent` minus running archive jobs minus dispatched jobs. With a `daily_limit`, it also queues at most `daily_limit` minus the daily count minus dispatched jobs. A dispatched job is a `queued` row with a procrastinate job. Outside the window or at the cap it queues nothing.
- `job.deferred_until` holds the next window start for a waiting job outside the window, the next 00:00 KST at the daily cap, and the later of the two when both hold. It is null when neither holds. The dispatcher sets it on every run.
- A dispatched job whose procrastinate job is no longer `todo` or `doing`, such as one that failed before the worker marked the row, goes back to waiting on the next dispatch. Otherwise it would hold its slot for good.
- The worker checks the window, the daily cap and the free slots again before it starts. If it may not start, it hands the job back to waiting, queues nothing, and logs `job.returned` with reason `paused`, `blocked`, `daily_limit`, `window` or `slots`, the first that holds. The check, the `running` mark, and the dispatch all take one Postgres advisory lock, so a slot is never taken twice.
- The worker deletes a procrastinate job when it succeeds, so `procrastinate_jobs` holds only todo, running, failed and cancelled jobs. The hourly database cleanup deletes the finished ones after 30 days (Resuming and verifying downloads). A `job` row keeps the history. A `job.queue_job_id` that names a deleted procrastinate job reads as "no longer `todo` or `doing`".
- A `PUT /api/download-settings` changes no job. The next dispatch, within 10 s, applies the new limits.
- Migration `0006` deletes every `succeeded` procrastinate job and every `todo` archive procrastinate job. It clears `queue_job_id` and `deferred_until` on every `queued` archive job whose procrastinate job is not `doing`, so the dispatcher takes it. A `doing` job and its `job` row stay. The downgrade cancels every waiting archive job, so the old poll queues its video again.
- Process health and job ownership are separate. Kubernetes judges health: worker and scheduler serve `GET /healthz` on `SAVETUBE_HEALTH_PORT` (default 8081) from their own asyncio loop, so a stuck loop fails the probe, and the chart sets a livenessProbe on it. The metrics port cannot serve this, because `prometheus_client` answers from its own thread while the loop hangs. Docker compose runs the same check as a `healthcheck`. Job ownership stays in Postgres as procrastinate's heartbeat lease.
- A YouTube block pauses archive downloads. A block is a `DownloadFailed` whose text, lowercased, holds both "confirm you" and "not a bot", or "http error 429". The bot check text has a straight or a curly apostrophe, so no marker includes it. `savetube.ytdl.BLOCK_MARKERS` holds the markers.
- A bare "Video unavailable" is a block only as the last of a streak. Bare means yt-dlp's text after `[youtube] <video id>: ` is "Video unavailable" with an optional period and nothing after it. A throttled client gets it for public videos, and a deleted video gets it too, so one alone fails the job. A deleted video usually adds a sentence, such as "This video has been removed by the uploader", and that text never counts.
- The streak holds when the 2 previous failed or succeeded archive jobs of other videos, newest by `finished_at`, all failed with a bare "Video unavailable" and finished within the last hour. `UNAVAILABLE_STREAK` (3) and `UNAVAILABLE_STREAK_WINDOW` in `savetube.jobs.job` set it. A success between them ends the streak. The job is then blocked like any other block, and its `interrupted` row's `log_tail` starts with "blocked by YouTube".
- A blocked archive job is interrupted, not failed: it counts as no attempt, keeps `.incoming/<video id>/` and its pinned format, and logs `job.interrupted` at WARNING with reason "blocked by YouTube". The `interrupted` row's `log_tail` holds the yt-dlp text.
- The first block past an expired pause adds 1 to `download_settings.block_count` and sets `blocked_until` to now plus the backoff, under the slot lock. The backoff is 30 min, doubling with each count, at most 12 h: 30 min, 1 h, 2 h, 4 h, 8 h, 12 h. It logs `dispatch.blocked` at WARNING with `until`, `backoff`, `count`, the job, the video, and the reason.
- A block while a pause holds, such as jobs refused together, only interrupts its job.
- While `blocked_until` is in the future, the dispatcher queues nothing and the worker hands a job back with `job.returned` reason "blocked". `job.deferred_until` holds the latest of `blocked_until`, the next window start, and the daily cap's reset.
- After the pause and until a download succeeds, `block_count` is above 0 and the effective `max_concurrent` is 1. One job tests whether YouTube still blocks.
- A succeeded archive job after the pause resets `block_count` to 0 and `blocked_until` to null, and logs `dispatch.unblocked` at INFO with the previous count. A success while a pause holds resets nothing, because that download started before the block.
- A live recording ignores blocks. A blocked recording fails like any other failure.
- Migration `0009` adds `blocked_until` and `block_count` and turns every `failed` archive job whose `log_tail` holds "not a bot" into `interrupted`, so a bot-check wave gives back the attempts it took. The downgrade drops the columns and keeps those jobs `interrupted`.
- The scheduler reclaims an archive job left `running` by a dead worker every minute. It reads procrastinate's stalled jobs: those of a worker with no heartbeat for 300 s (`STALLED_HEARTBEAT_SECONDS` in `savetube.scheduler`), and those with no worker. The job is interrupted with `log_tail` "worker stopped during the download" and logs `job.interrupted` at WARNING. Without this, a dead worker's jobs hold download slots forever.
- The 300 s threshold keeps a short database outage, such as a Postgres failover, from reclaiming a live worker's jobs. procrastinate's default is 30 s. A worker prunes another whose heartbeat is 30 s old, and the pruned worker's jobs have no worker, so the reclaim takes them at any threshold.
- A run checks that its job row is still its own: same id, state `running`, and the `started_at` the run set. A reclaim sets the row back to `queued`, and the next run sets a new `started_at`. The 5 s pause read checks it while the download runs, and the run checks it again, with the row locked, before the move into the tree and before it writes an interruption or a failure. A progress write applies only to a row the run owns, and a format pin is written only after an ownership read finds the row still its own.
- A run that lost its row writes nothing: no job row, no `interrupted` row, no video state, no pin. It neither deletes nor moves `.incoming/<video id>/`, which the next run may use. A running download is cancelled as a pause cancels it: the child is killed and its `.part` files stay. The run logs `job.disowned` at WARNING with the job, the video, and the reason. A failed database read during the download logs `job.pause_check_failed` and the download goes on.

| Method and path | Body | Returns |
| --- | --- | --- |
| `GET /api/download-settings` | | `{max_concurrent, rate_limit_bytes, window_start, window_end, window_enabled, daily_limit, cookie_profile, timezone, blocked_until, block_count, paused, downloaded_today}`. The last four are read-only here. `downloaded_today` is the daily count |
| `PUT /api/download-settings` | `{max_concurrent, rate_limit_bytes, window_start, window_end, window_enabled, daily_limit, cookie_profile}`. A `daily_limit` below 1, or `window_enabled` true with a missing time, is a 422. A `cookie_profile` the scheduler has not recorded as present is a 422 | the settings |
| `PUT /api/download-settings/paused` | `{paused}` | the settings |
| `GET /api/cookie-profiles` | | `[{name, account_name, account_handle}]`, the profiles the scheduler last recorded, by name, with the account each file signs in, null until a read succeeds. Never their content |
| `GET /api/jobs` | | each job also carries `deferred_until` |
| `PATCH /api/sources/{id}` | also `archiving` | the source, with `archiving` |

## Resuming and verifying downloads

- An archive download works in `.incoming/<video id>/`, or in `incoming/<video id>/` on staging (storage.md, Staging), so every job for one video uses the same directory. A recording keeps `.incoming/<job id>/` and starts from the first segment on every attempt.
- An interrupted run is one the worker stopped: a worker shutdown, a reclaim after the worker died, a YouTube block, or a network error. It counts as no attempt. Only `failed` jobs count toward `MAX_ARCHIVE_ATTEMPTS`.
- An interruption keeps the job's id, so the job keeps its place in the queue. The run that started becomes its own `job` row with state `interrupted`, its start and end time, and the reason in `log_tail`. The job goes back to `queued` with no procrastinate job and `attempts` back where it was, so the dispatcher queues it again within 10 s. A dispatched job that no worker started goes back to waiting with no `interrupted` row.
- An interruption keeps `.incoming/<video id>/`. The video goes back to `pending`.
- A network error interrupts an archive run: yt-dlp text such as "Read timed out", "bytes read, ... more expected", "Connection reset", "Connection refused", "Remote end closed", "IncompleteRead", or "Network is unreachable" (`NETWORK_MARKERS` in `savetube.ytdl`). The run keeps the directory and `video.resume_format` and logs `job.interrupted` at WARNING with the bytes the run added. It does not pause downloads. A YouTube block match wins over a network match.
- yt-dlp first retries inside the run: `build_params` sets the CLI defaults `retries` 10, `fragment_retries` 10, `extractor_retries` 3 and `file_access_retries` 3. As a library, yt-dlp retries nothing without them. An HTTP retry continues the `.part` file with a Range request. Only an error past these retries reaches the network interrupt.
- The interrupted job keeps its id, so the dispatcher takes the same video again before any newer job and the download continues from its parts.
- A run that grew `.incoming/<video id>/` writes `log_tail` "network error: <yt-dlp text>". A run that did not writes "network error, no progress: <yt-dlp text>".
- A download that keeps adding bytes is retried until it ends. After 5 runs in a row that added no bytes (`MAX_NETWORK_STALLS`), the next one fails the job. The count holds the "no progress" rows since the video's last `failed` or `succeeded` archive job or "network error" row, ordered by `finished_at`.
- On SIGTERM the worker stops taking jobs and cancels its downloads at once (`shutdown_graceful_timeout=0`). A cancel kills the download child, whose `.part` files stay, marks the job interrupted, and logs `job.interrupted` at INFO. The child ignores SIGINT and SIGTERM, so only the parent ends a download.
- The worker's `terminationGracePeriodSeconds` is 30. A shutdown takes a few seconds, one kill and one database write per download. A worker still running at the deadline is killed, and the reclaim interrupts its jobs once procrastinate sees its heartbeat stop.
- `download.format` stores the chosen format ids, such as `137+140`, in `video.resume_format`. The child sends them to the parent through the progress pipe.
- A retry with a pinned format and an existing directory passes the pinned format ids to yt-dlp instead of the channel's selector, with `continuedl` on, so yt-dlp continues each `.part` file. It logs `download.resume` with the bytes in the directory and in `.part` files.
- A retry skips the chat replay when `<video id>.live_chat.json` is in the directory or its `.tmp/`, by adding `-live_chat` to `subtitleslangs`. yt-dlp rewrites every subtitle it finds, and a chat replay cannot resume. A finished chat has its final name, because yt-dlp renames it from its `.part` at the end.
- When yt-dlp answers "Requested format is not available" to the pinned format, the run logs `download.restart` at WARNING, deletes the directory and the pin, and downloads again with the channel's selector in the same attempt.
- googlevideo answers HTTP 500 to every request for some formats of some videos, from any network. yt-dlp then fails with "HTTP Error 500 ... Giving up after N retries" (`format_server_error` in `savetube.ytdl`).
- The HLS copy of such a format, such as `312` for `299`, fails the same way on every fragment. yt-dlp skips each fragment and fails with "The downloaded file is empty", which counts as the same error.
- When that error ends a run that added no bytes to any `.part` file or `.part-Frag<n>` fragment, the run downloads again in the same attempt with the failing format id excluded from the channel's selector. The failing format is the first id of the chosen format, such as `299` in `299+251`, that sent no finished-stream event, because yt-dlp downloads the streams in that order.
- The exclusion wraps the selector in a group with one filter per refused id, such as `(bv*+ba/b)[format_id!='299']`. The filter drops the format before every alternative inside the group picks. The id is quoted, because yt-dlp reads an unquoted `format_id!=299` as a number comparison that keeps every format.
- One attempt excludes at most 3 formats (`MAX_FORMAT_FALLBACKS`). The next refusal fails the job as before. A refusal is neither a network interruption nor a YouTube block.
- Each exclusion logs `download.fallback` at WARNING with the excluded id and the format the next run picks. The next run pins its own format, and its `.part` files of other formats continue.
- A pin without its directory is cleared, and the download starts with the channel's selector.
- A succeeded download moves the directory into the tree, or into `ready/` on staging, and clears the pin. A failed one deletes the directory and clears the pin.
- The scheduler deletes, every hour, a directory under `.incoming/` or staging's `incoming/` with no file written for 7 days whose name is neither the video of a queued or running archive job nor the id of a queued or running job. It clears the `resume_format` pin of the video a deleted directory belongs to, and logs `incoming.deleted`. Seven days is longer than any wait for a download window, and a waiting job's directory is never deleted. Cancelled jobs and a video whose source stops archiving leave such directories.
- The scheduler also cleans the database every hour (`savetube.cleanup`). It clears `job.progress` on every job that is not `running`. It deletes a procrastinate job in a finished status (`succeeded`, `failed`, `cancelled`, `aborted`) whose last event is older than 30 days, and its events with it. It deletes the `interrupted` archive rows of a video whose latest succeeded archive job finished more than 30 days ago. Succeeded, failed and cancelled rows stay, and so do the `interrupted` rows of a video not yet archived. It logs `cleanup.done` with the counts `progress`, `queue_jobs` and `interrupted`, only when one is above 0.
- Before the move into the tree, `savetube.integrity` checks the download:
  - size: each stream file before the merge has the size of its format's exact `filesize`. A format with only `filesize_approx` is not checked. The child reports each finished stream's size, because the merge deletes the stream files.
  - probe: `ffprobe` reads the final file. Its duration is within the larger of 2 s and 1% of the info dict's `duration`, when the info dict has one. It has a video stream when the format's `vcodec` is not `none`, and an audio stream when its `acodec` is not `none`.
  - packets: a resumed download also gets `ffmpeg -v error -xerror -i <file> -c copy -f null -`. A stream copy decodes nothing, so a multi-hour file takes about as long as reading it. Any error output fails the check.
- Without `ffprobe` or `ffmpeg` on the path, probe and packets are skipped with a DEBUG line. The image carries both.
- A failed check logs `download.integrity_failed` at WARNING with the check and its numbers. The run deletes the directory and the pin and downloads from zero once, in the same attempt. A second failure fails the job with `log_tail` "integrity check failed: <check> ...", which counts as an attempt.
- A passed download logs `download.verified` with the checks that ran. `video.verified_at` and `video.verified_checks` hold the result, and `GET /api/videos` and `GET /api/videos/{id}` carry both.
- The channel's Downloads list shows `interrupted` rows among the finished jobs.

## Audio copy

- Every archived video gets an audio-only file in its directory, named `<video id>.<ext>` like the video file. No extra YouTube request is made.
- After a download passes its integrity check, `savetube.audio` copies the first audio stream of the merged file with `ffmpeg -map 0:a:0 -c copy`. Nothing is re-encoded.
- The extension follows the audio codec that `ffprobe` reports: `opus` gives `.opus`, `aac` gives `.m4a`, and any other codec gives `.mka`.
- ffmpeg writes to `<name>.tmp` and the file is renamed when ffmpeg succeeds, so a crash never leaves a half file under the final name.
- The copy is written in `.incoming/<video id>/` before the move, so it moves into the tree with the video. `video.size_bytes` counts it (frontend.md).
- A file with no video stream, such as a `ba/b` download, or with no audio stream gets no copy. The worker logs `audio.skipped` at DEBUG.
- A failed copy logs `audio.failed` at WARNING with the reason. The job still succeeds and the video stays `complete`.
- A live recording gets no copy.
- `savetube extract-audio` writes the missing copy of every `complete` video, except a video with a succeeded `record` job. It finds the video file as `<dir_path>/<video id>.<info ext>` under the archive root.
- A video that already has a `.opus`, `.m4a` or `.mka` file beside its video file is skipped, so a second run writes nothing.
- The command prints one line per video and a summary line, and continues past a failed video. It updates `video.size_bytes` after each copy. It exits 1 when any video failed.
- `--dry-run` probes each video and prints the file it would write, and writes no file and no row.
