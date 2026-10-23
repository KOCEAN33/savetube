# Candidates

Work not scheduled yet. An item leaves when it is built or rejected.

## Open questions

- Members-only videos get a Members tab only while the channel has some. The user has not confirmed this.
- Does a rate limit raise googlevideo drops? Drops cluster at fixed byte offsets of one video (`JXx51sPgPCs`: 42.1 MiB, 1.15 GiB, 1.59 GiB), with and without the limit. Answering it needs a run with the limit on, compared per video with these offsets.

## Needs the user

- Log format: the user wants to review it. Logs are logfmt by the decision in `docs/plan/operations.md` Logging. A switch to JSON changes that section, `backend/savetube/logs.py`, and any Loki query that uses `| logfmt`.
- Smaller image: ffmpeg is about 180-210 MB of it. A smaller one means a static 8.x or 9.x build or a slim build from source, both with fewer codecs. Needs the user's approval.
- Safari playback: out of scope until the user asks. The plan then is a cached mp4 remux with AAC audio, chosen by `canPlayType('video/x-matroska')`.
- i18n with Korean beside English.

## Bugs and gaps

- A recorder whose stream stays upcoming past `scheduled_at`, such as a cancelled waiting room, waits with `wait_for_video` until someone deletes its Job.
- When the poll queues the VOD while a recording still merges, both target one tree directory, and the second `finalize` fails with `FileExistsError`.
- A failed audio extraction after a download is not retried. Only a rerun of `savetube extract-audio` through `kubectl exec` fills it, with `print` output invisible in Loki and tied to the caller's connection.
- The channel page hides "Show more" once all 500 listed queued jobs show, even when more wait.
- `GET /api/sources` sends `last_error` raw. The Home card cuts it at 120 characters, without the yt-dlp noise stripping `job_reason` does.
- The worker swallows the finish error for any queue row already `failed`. Only the reclaim fails one today.
- `PUT /api/download-settings` defaults `window_enabled` to false, so a client that omits it turns the window off. The Settings page always sends it.
- `/api/stats/daily` buckets by the DB session time zone, not KST, so it can differ from `downloaded_today` near midnight.
- The format fallback treats any HLS run that ends "The downloaded file is empty" with no bytes as a refused format, whatever the cause. The cap of 3 per attempt bounds it.
- `POST /api/channels` refuses `watch_lives` without a tab, but `PATCH /api/channels/{id}` sets it with no such check (`web/api/channels.py`).
- The GET and HEAD routes for `/media` and `/audio` share an operation ID. FastAPI warns "Duplicate Operation ID", and which method names it depends on the hash seed.
- A playlist listing can name the group channel for a member's video, which leaves an empty group channel row once the download stores the video under the member.

## Cleanup

- The settings page uses a checkbox for the window, because the project has no Switch component.
- Test files import helpers with `from conftest import`. A plain helper module such as `tests/_db.py` would be cleaner.
- `savetube/__init__.py` imports `savetube.cli`, so importing any `savetube.X` loads cli, config and db.
- `jobs/guards.py` and `jobs/job.py` import private names (`_interrupt`, `_owner_lost` and others) from sibling modules.
- pyright flags `jobs/job.py` `failed + 1`, where `session.scalar` can return None. pyright is not in `make lint`.
- `frontend/src/pages/channel/archive-section.tsx` is 450 lines.
- The "Audio only" player is too narrow to seek comfortably. The `<audio>` in `frontend/src/components/video-player.tsx` sets no width, so Chrome draws its default width. Low priority, because the video player is the main view.
