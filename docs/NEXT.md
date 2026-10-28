# Next

## Goal

Confirm `v0.13.0` in production: the Downloads page, the `members_all` and `playlists_pass` switches, and the `main` cookie profile.

## Next steps

1. After the next CULUA poll, check that `membership` holds a `main` row for CULUA and that Settings, Memberships shows it.
2. The user checks Home, the Downloads page, the channel tabs and the Settings membership table on `savetube.lab.hiriri.com`.
3. When the user adds a channel with `playlists_pass` on, check in `poll.done` that its playlist entries queue without the include words.
4. Production has never run the format fallback. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
5. `3d38f29`, the member-profile retry for members-only shorts, is in production since `v0.17.0`. Check that the KMNCLUB shorts `B_jIZdzTAQU` and `fnE7PCjieuI` (channel `UC68J5pWEshmwhDH45qnpIsg`) log `job.member_retry` and end `succeeded` with `type=members`.
6. `17d3ead`, the bare "Video unavailable" streak block, is in production since `v0.18.0`. When the worker log shows `dispatch.blocked` with a bare "Video unavailable" reason, check that the job is `interrupted` with a `log_tail` starting "blocked by YouTube" and that `download_settings.blocked_until` is set.
7. The per-channel counts in `GET /api/channels` and `GET /api/stats/summary` join `_channel_videos()`, which takes their production query from 6.7 s to 5 ms. In `v0.24.1`, check that Home and the sidebar load at once, and that the channel table's counts match the sidebar's. Connections are a separate finding: pgbouncer `show pools` lists 21 savetube clients but only 5 server connections. The clients are SQLAlchemy pools held open by the web, scheduler, worker and recorder pods, and transaction pooling keeps the server side small.
8. `v0.18.1` runs the format watch (`docs/plan/live.md`, Format watch) on 6 recent lives. Its migration cancelled the queued replace of `WBJeEquyZ2s`. At the next 6-hourly probe, check that `watch.queued` requeues `WBJeEquyZ2s` and `hU7U_HnNWUo` for audio `251` only, and that the replace ends `succeeded` with the AAC file in `.originals/<video id>/` and a new `.opus` copy beside the video.
9. Onboarding a channel whose uploads are all members-only. The user is a member, but the add-channel scan offers no Videos or Shorts tab, because `POST /api/channel-scan` lists without cookies and sees no entries. With no tab chosen, no poll lists a members-only entry, so the membership check never runs either. The user wants the onboarding to check membership and list the tabs with the member profile. The web pod never mounts the cookie Secret (`docs/plan/access.md`, Cookies and account), so the scheduler does the member listing and the check.
   - Decision: a user action that asks the scheduler to act now goes over HTTP, never through a DB flag the scheduler loop picks up. The procrastinate queue stays in Postgres.
   - Build: add the routes to the scheduler's internal API, `backend/savetube/scheduler_api.py` behind the ClusterIP Service `savetube-scheduler` (`docs/plan/runtime.md`, Scheduler API).
   - The onboarding call: `POST /api/channel-scan` calls the scheduler, which lists the tabs with the cookie profiles, checks membership on a members-only entry, stores `membership`, and answers within the scan's 120 s timeout. This lists with the real account, which `docs/plan/access.md` keeps for members-only downloads today, so that section changes.
   - Update `docs/plan/access.md`, `docs/plan/frontend.md` (Add channel) and `docs/plan/operations.md` with the API.
10. `v0.21.0` ships `9a21b21`, the manual format probe and replace for any complete video. After its deploy, check these things on a non-live video and on a live whose watch ended:
    - Probe now on the Formats card answers within 120 s and logs `watch.probed` in the scheduler.
    - A Use pick makes a replace job with `job.manual` true. The worker starts it ahead of the queued archive jobs.
    - The swap sets `format_watch.original_until` to 7 days after the swap.
11. `v0.21.0` ships `895a3f5` and `37dc2b3`, the slimmer Live page and the one short time form. After its deploy, check on the Live page while a live records:
    - The Recording now card shows no pod badge for a healthy recorder, and the Streams row shows the recorder Job number in the Recorder column.
    - The Streams table fits the screen without sideways scrolling, and no page shows a time with seconds.
    - `GET /api/pods` answers `{available, recorders}` with no `workers`.
12. In `v0.21.0`, Poll now, turning `archiving` on, and Move now call the scheduler's API (`docs/plan/runtime.md`, Scheduler API). Migration `0024` drops `download_settings.move_requested_at`. After its deploy, check these things:
    - Poll now on a channel source logs `poll.requested` in the scheduler within seconds, unless a long poll runs.
    - Move now while videos wait on staging logs `staging.move` with `reason=manual` within seconds.
    - The web log holds no `source.poll_request_failed` or `staging.move_request_failed`.
    - The NetworkPolicy `savetube-scheduler` lets the web through and blocks other pods. Poll now succeeding covers the web. From the worker, `kubectl exec deploy/savetube-worker -c worker -- python -c "import urllib.request; urllib.request.urlopen('http://savetube-scheduler:8082/poll/0', data=b'', timeout=5)"` must time out. The scheduler pod stays `Running` with no liveness restarts.
