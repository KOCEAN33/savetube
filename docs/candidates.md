# Candidates

Work not scheduled yet. An item leaves when it is built or rejected.

## Open questions

- Members-only videos get a Members tab only while the channel has some. The user has not confirmed this.
- Does a rate limit raise googlevideo drops? Drops cluster at fixed byte offsets of one video (`JXx51sPgPCs`: 42.1 MiB, 1.15 GiB, 1.59 GiB), with and without the limit. Answering it needs a run with the limit on, compared per video with these offsets.

- Do the database connections need tuning? pgbouncer `show pools` lists 21 savetube clients but only 5 server connections. The clients are SQLAlchemy pools held open by the web, scheduler, worker and recorder pods, and transaction pooling keeps the server side small.

## Needs the user

- Format watch video pick: the codec rule ranks AV1 `399` above the premium VP9 `616`, which has a higher bitrate. A live stored as `616` would be replaced by `399`. Whether `616` wins needs the user's call.
- Log format: the user wants to review it. Logs are logfmt by the decision in `docs/plan/operations.md` Logging. A switch to JSON changes that section, `backend/savetube/logs.py`, and any Loki query that uses `| logfmt`.
- Smaller image: ffmpeg is about 180-210 MB of it. A smaller one means a static 8.x or 9.x build or a slim build from source, both with fewer codecs. Needs the user's approval.
- Safari playback: out of scope until the user asks. The plan then is a cached mp4 remux with AAC audio, chosen by `canPlayType('video/x-matroska')`.
- i18n with Korean beside English.
- Quality tiers. The user wants 1080p as the default, AV1 first, and the highest resolution only for 3D lives and concert-style "LIVE" events, not for every past stream. This changes requirement 1 in `docs/plan/README.md`. Two code paths pick by resolution first: `FORMAT_SORT` in `ytdl.py` (a default tier would use `res:1080`) and `pick_video` in `formats.py`, which only counts formats at least as tall as the stored one, so the watch never swaps a 1440p file for a 1080p AV1. The default tier needs the watch to treat a stored file above 1080p as replaceable, which re-downloads the few WQHD singing streams. How a video joins the max tier is open: title words like the include words, a per-video switch, or both. The user wants to study real titles first. Recordings keep yt-dlp's order.
- Members-only onboarding check in production, waiting on the user to scan and add a channel whose uploads are all members-only (from the handoff). The scan answers within 120 s, offers the Videos tab, and shows a members-only badge for `main` with "member". The scheduler log shows `membership.checked` per profile and no `scan.profile_failed`, and the web log holds no `scan.request_failed`. After the add, the scheduler log shows `poll.member_listing` with `profile=main`, then `poll.done` with the members-only entries in `listed` and `queued`, and no `skip_members`. A `poll.failed` with "does not have a videos tab" and no `poll.member_listing` means the member listing did not run: check the channel's membership result and `blocked_until`.
- Poll now check in production, waiting on the user to press Poll now on a channel source (from the handoff). The scheduler logs `poll.requested` within seconds and the web log holds no `source.poll_request_failed`.

## Bugs and gaps

- A failed audio extraction after a download is not retried. Only a rerun of `savetube extract-audio` through `kubectl exec` fills it, with `print` output invisible in Loki and tied to the caller's connection.
- The worker swallows the finish error for any queue row already `failed`. Only the reclaim fails one today.
- The format fallback treats any HLS run that ends "The downloaded file is empty" with no bytes as a refused format, whatever the cause. The cap of 3 per attempt bounds it.
- A playlist listing can name the group channel for a member's video, which leaves an empty group channel row once the download stores the video under the member.
- The scheduler logs `live.check_failed reason="HTTPError: HTTP Error 404: Not Found"` every minute for several channels, VESPERBELL and 猫汰つな among them. The cause is not investigated.
- After a profile pause ends, every waiting job of that profile may dispatch at once, and each refusal costs a quick run. The YouTube block runs one probe job instead (`concurrency`).
- The replace run's 403 client fallback and profile block have no test, and migration `0030` has none either.

## Cleanup

- Test files import helpers with `from conftest import`. A plain helper module such as `tests/_db.py` would be cleaner.
- `savetube/__init__.py` imports `savetube.cli`, so importing any `savetube.X` loads cli, config and db.
- `jobs/guards.py` and `jobs/job.py` import private names (`_interrupt`, `_owner_lost` and others) from sibling modules.
- pyright flags `jobs/job.py` `failed + 1`, where `session.scalar` can return None. pyright is not in `make lint`.
- `frontend/src/pages/channel/archive-section.tsx` is 450 lines.
- The "Audio only" player is too narrow to seek comfortably. The `<audio>` in `frontend/src/components/video-player.tsx` sets no width, so Chrome draws its default width. Low priority, because the video player is the main view.
