# Archive

## Source polling

- A source is one channel tab (`videos`, `shorts`, `streams`), one channel's playlists (`playlists`), or one playlist, stored as a canonical URL. Adding a bare channel URL creates one source per chosen tab.
- The scheduler lists a due source with yt-dlp's flat extraction: ids and titles, no request per video. Listing a channel with a few thousand videos takes minutes, because every page request sleeps 0.75 s.
- A tab's entries carry no `channel_id`, so the poll takes it from the listing. Playlist entries carry their own, and a private or deleted entry without one is skipped.
- Every listed video that has a `video` row gets a `video_source` link, whether or not it passes the filters. A video that passes and is not yet stored gets a `pending` `video` row and an archive job, unless a job for it is queued or running, or three archive jobs for it have failed.
- `is_live`, `is_upcoming` and `post_live` entries are left to live detection.
- The channel's date cutoff reads the entry's approximate timestamp ("3 weeks ago"), so it is only as exact as YouTube's relative date. A video uploaded a day before the cutoff can pass.
- A failed archive job deletes its `.incoming/<job id>/`, because the retry is a new job with a new directory.
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
| `GET /api/channels` | | each channel also carries `include_words`, `exclude_words`, `date_after`, `format`, `use_cookies`, `watch_lives` |
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
- An archive task that starts outside the window, or with no free slot, defers its procrastinate job and leaves the `job` row `queued`. Outside the window it defers to the next window start. Without a free slot it defers 60 s. `job.deferred_until` shows the time. The slot check takes a Postgres advisory lock, so two workers cannot both take the last slot.
- A `PUT /api/download-settings` reschedules every deferred job, so a widened or removed window takes effect at once instead of at the old window start.
- Process health and job ownership are separate. Kubernetes judges health: worker and scheduler serve `GET /healthz` on `SAVETUBE_HEALTH_PORT` (default 8081) from their own asyncio loop, so a stuck loop fails the probe, and the chart sets a livenessProbe on it. The metrics port cannot serve this, because `prometheus_client` answers from its own thread while the loop hangs. Docker compose runs the same check as a `healthcheck`. Job ownership stays in Postgres as procrastinate's heartbeat lease.
- The scheduler reclaims an archive job left `running` by a dead worker every minute. It reads procrastinate's stalled jobs, found by the worker heartbeat. The `job` row becomes `failed` with `log_tail` "worker stopped during the download", and its `.incoming/<job id>/` is deleted. Without this, a dead worker's jobs hold download slots forever. The next poll queues the video again, within `MAX_ARCHIVE_ATTEMPTS`.

| Method and path | Body | Returns |
| --- | --- | --- |
| `GET /api/download-settings` | | `{max_concurrent, rate_limit_bytes, window_start, window_end, timezone}` |
| `PUT /api/download-settings` | `{max_concurrent, rate_limit_bytes, window_start, window_end}` | the settings |
| `GET /api/jobs` | | each job also carries `deferred_until` |
| `PATCH /api/sources/{id}` | also `archiving` | the source, with `archiving` |
