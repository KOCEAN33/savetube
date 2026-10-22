# Next

## Goal

The no-behavior-change refactor of the backend and the frontend is released as `v0.12.1`. Production checks wait for the user: the step label on the Downloads card, the HTTP 500 format fallback, playback and subtitles.

## Current progress

- Released: `v0.12.1`, the refactor, 14:43 KST on 2026-10-22 through PR #41, no migration. Argo CD deploys it. `v0.12.0` (PR #40, migrations `0001`-`0015`) ran before it, and the user confirmed that Move now moves staged videos at once. A merge to develop deploys nothing. A merge of develop into main releases and deploys (`docs/plan/operations.md` Versioning). The release workflow run appears about 85 s after the merge.
- `v0.11.0` added the step label on the Downloads card after the last byte (merging, checking the file, writing the audio copy). `job.progress` then holds `{step}` (`docs/plan/storage.md`).
- `v0.11.0` added staging (`docs/plan/storage.md`, Staging). Archive downloads work on the 40Gi `nvme` PVC `savetube-staging` and move into the archive in batches: at 20 GiB staged, below 15 GiB free, or after 1 hour with no download running. Staged videos stay hidden until moved. Checked after the deploy: the PVC is Bound, `/staging` belongs to 1000:1000, the worker has `SAVETUBE_STAGING_ROOT=/staging`, alembic is `0014`.
- Checked after the `v0.10.0` deploy: the web pod mounts the archive read-only (`touch` fails with "Read-only file system"), and `GET /api/videos/<id>/media` without a session answers 401.
- Download settings in production: window off with no times stored, `daily_limit` null, `max_concurrent` 1, general cookie profile `burner`. The user turns the window on in Settings ("Use the window").
- `v0.9.0` and `v0.10.0` added:
  - HTTP 500 format fallback (`docs/plan/archive.md`, Resuming and verifying downloads). Trigger: video `7l-RzDkD7a0` answers 500 for format 299 (1080p60 DASH) from any network, and its HLS copy 312 ends "The downloaded file is empty". 298 (720p60) works. Its job is queued and has not run on the new code yet.
  - Audio copy beside every video (`docs/plan/archive.md`, Audio copy). The backfill wrote 239 `.opus` files, 18.4 GB, 0 failed. Every complete video has one. The first worker extraction in production (`MSS6tecJ5iw`, 160 MiB) logged `audio.extracted`.
  - Daily download cap and window switch (`docs/plan/archive.md`, Download limits).
  - Playback: `GET|HEAD /api/videos/{id}/media` and `/audio`, native `<video controls>`, Chrome only (`docs/plan/frontend.md`, Video page). Chrome 156 plays and seeks av01, avc1 and vp09 mkv with Opus over Range. Samples were 3-4 minute videos, not multi-hour lives.
  - Subtitles: `GET /api/videos/{id}/subtitles` and `/subtitles/{lang}`, default language per browser in localStorage. Only one production video has `.vtt` files (`pjiTdkZ9DoI`, 7 languages). 237 videos have `live_chat.json`, which the user does not want shown.
- Overnight 2026-10-22 findings: googlevideo still drops connections with yt-dlp retrying in place. Drops cluster at fixed byte offsets of one video (`JXx51sPgPCs`: 42.1 MiB, 1.15 GiB, 1.59 GiB), with and without the rate limit, so the rate limit is not shown to be the cause. The interrupt loop finished that video.
- Lives without AV1: YouTube offers only H.264 at 720p and 1080p for many lives (148 of 225 stored lives). yt-dlp picks AV1 when it exists at the same resolution and fps.
- `savetube_proto` on pg-dev holds seeded demo data. Its complete videos have no `dir_path` or `info`, so playback tests must point a row at files and revert it after. `/tmp/savetube-library/serve.sh` serves a worktree build against it, login skipped. `/tmp/savetube-playback/serve.sh` and `/tmp/savetube-subs/serve.sh` add an archive root.
- CULUA is the one production channel.
- `v0.12.0` added Move now on the Downloads card staging line (`POST /api/staging/move`, migration `0015`). The web cannot move files, so it sets `download_settings.move_requested_at` and the scheduler moves within a minute.
- `v0.12.0` added the Videos channel filter that lists each name once and filters by `channel_name`. The user chose grouping by the shared name: group member channels such as KMNZ's (@KMNZOFFICIAL, @KMNZOFFICIAL_TINAS, @KMNZOFFICIAL_TINAM) have their own ids but carry the group's name in their video info. The user expects RK Music's other units to follow the same pattern. A playlist listing can name the group channel for a member's video, which leaves an empty group channel row once the download stores the video under the member. Inside a name group it is harmless.
- `v0.12.1` holds the refactor. `web/api/` and `jobs/` are packages whose `__init__.py` re-exports the old public names. Tests patch a name on the submodule whose call site reads it (`jobs.runners.download`, `jobs.guards.PAUSE_POLL_SECONDS`, `web.api.scan.CHANNEL_PLAYLISTS_TIMEOUT`). Modules in `jobs/` name their logger `savetube.jobs` and `web/api/deps.py` names it `savetube.web.api`, so the `logger=` field stays. `TIMEZONE` lives in `savetube/tz.py`. `cli.py` keeps lazy imports on purpose: `savetube.queue` reads Settings at import, and the rest are role-specific heavy packages. `poll.sync` and `live.check_channel` share only `get_or_create_channel`; every other stage differs in behavior. DB tests live in `backend/tests/test_db_*.py` with shared fixtures and helpers in `conftest.py`.

## What worked

- DB tests: `bash /tmp/savetube-testdb/run-tests.sh <repo root> make check`, or pytest args instead of `make check`. It reads `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` and the password from `op read 'op://Homelab/savetube-dev-db/password'`. It prints no secret. Parallel runs are safe. Tests that need ffmpeg skip on this Mac. Run them with the shims in `/tmp/savetube-ffshim` first on PATH, which call the local `savetube:after` image. The shims mount only `/private/tmp`, so add `--basetemp=/private/tmp/<dir>` to the pytest args, or the fixtures' ffmpeg fails with exit 254.
- Refactor guards: `backend/tests/test_e2e.py` runs uvicorn and the real `savetube worker` with `yt_dlp.YoutubeDL` faked through a `sitecustomize.py` on `PYTHONPATH`, and fails when an `/api` route has no call. `make e2e-smoke` (through `run-tests.sh`) runs Playwright over 6 pages and needs the ids, aria-labels, roles and URL params it targets to stay. After the refactor: pytest with the ffmpeg shims 371 passed, 0 skipped; smoke 6/6; frontend lint 0 errors, 7 warnings.
- `psql` 18 is installed through Homebrew `libpq`.
- Parallel agents: make the worktrees yourself with `git worktree add -b <branch> ../savetube-wt-<name> origin/develop` and give each a file boundary. `CLAUDE.md` Merging says how a branch lands.
- `.claude/settings.local.json` allows `gh pr merge`, `git rebase`, `git merge`, `git pull`, and `git push origin develop`. Without them the auto mode classifier blocks them as "Merge Without Review". It also blocked a read-only `gh pr view` loop right after a merge.
- Production reads: `kubectl --context homelab-tailnet -n postgres exec postgres-2 -c postgres -- psql -d savetube -c "set default_transaction_read_only=on;" -c "<one query>"`. One query per call passes the permission classifier. `kubectl exec deploy/savetube-worker -c worker -- <cmd>` runs commands such as `find /archive` and `savetube extract-audio`.
- yt-dlp repros: the local image `savetube:after` holds the worker's yt-dlp and ffmpeg: `docker run --rm --entrypoint sh -v <empty dir>:/out savetube:after -c "cd /out && yt-dlp ..."`. Host `uvx yt-dlp` lacks a JS challenge solver and may miss formats.
- Local UI checks: Playwright through `uvx --with playwright python -I <script>` with `channel="chrome"`. Frontend uses pnpm (`pnpm run typecheck`, `build`, `lint`).

## What did not work

- Copying an archive file out of the worker pod (`kubectl exec ... cat`) is denied by the permission classifier as "Production Reads". Download the same video with the local image instead.
- A command run through `kubectl exec` writes nothing to pod logs or Loki, and Python buffers its stdout until exit.
- The harness `isolation: worktree` starts from an old main merge commit, not develop.
- Do not watch a deploy with kubectl. Argo CD deploys a release through webhooks, and the user does not want it polled. A kubectl polling loop is also denied by the permission classifier as "Production Deploy".
- `bsk` on `savetube.lab.hiriri.com` fails: the page holds another extension's frame. The user checks the production UI.
- An Argo CD refresh annotation is denied by the permission classifier. The webhooks make it unnecessary.
- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`.
- pytest-cov fails 200 tests with `TypeError: 'InternalTraversal' object is not callable` from SQLAlchemy's Cython `_cache_key_cy.py`, with every coverage core. `DISABLE_SQLALCHEMY_CEXT_RUNTIME=1` does not help. A `sys.monitoring` line collector plugin works: `/tmp/wt-run-split-plugin/linecov_plugin.py`, loaded with `-p linecov_plugin`.
- `make talos-config` fails off the home LAN. Use `talosctl patch mc --mode no-reboot` over the tailnet.

## Open questions

- Members-only videos get a Members tab only while the channel has some. The user has not confirmed this.
- Does a rate limit raise googlevideo drops? It needs a run with the limit on, compared per video with the overnight 2026-10-22 numbers.

## Next steps

1. Check the step label on the Downloads card through merge, verify and the audio copy.
2. When `7l-RzDkD7a0` runs, check the worker log for `download.fallback` (299, then 312, then 298+251) and `job.done`.
3. The user checks playback, the audio player and subtitles on `savetube.lab.hiriri.com` in Chrome, including a multi-hour live and the ⋮ captions menu, which no test clicked.
4. Batch work is on hold: the user expects no more batch jobs. `savetube extract-audio` runs through `kubectl exec` with `print` output, invisible in Loki and tied to the caller's connection. A failed audio extraction after a download is not retried, and only a rerun of `extract-audio` fills it.
5. Follow-ups:
   - The channel page hides "Show more" once all 500 listed queued jobs show, even when more wait.
   - `GET /api/sources` sends `last_error` raw. The Home card only cuts it at 120 characters, without the yt-dlp noise stripping `job_reason` does.
   - The worker swallows the finish error for any queue row already `failed`. Only the reclaim fails one today.
   - The user wants to review the log format. Logs are logfmt, not JSON, by the decision in `docs/plan/operations.md` Logging: plain text in a terminal, and Alloy and LogQL `| logfmt` parse it. A switch to JSON changes that section, `backend/savetube/logs.py`, and any Loki query that uses `| logfmt`.
   - `PUT /api/download-settings` defaults `window_enabled` to false, so a client that omits it turns the window off. The Settings page always sends it.
   - `/api/stats/daily` buckets by the DB session time zone, not KST, so it can differ from `downloaded_today` near midnight.
   - The fallback treats any HLS run that ends "The downloaded file is empty" with no bytes as a refused format, whatever the cause. The cap of 3 per attempt bounds it.
   - The settings page uses a checkbox for the window, because the project has no Switch component.
   - `POST /api/channels` refuses `watch_lives` without a tab ("recording lives needs a tab"), but `PATCH /api/channels/{id}` sets it with no such check (`web/api/channels.py`).
   - The GET and HEAD routes for `/media` and `/audio` share an operation ID. FastAPI warns "Duplicate Operation ID", and which method names it depends on the hash seed.
   - Test files import helpers with `from conftest import`. A plain helper module such as `tests/_db.py` would be cleaner.
   - `savetube/__init__.py` imports `savetube.cli`, so importing any `savetube.X` loads cli, config and db.
   - `jobs/guards.py` and `jobs/job.py` import private names (`_interrupt`, `_owner_lost` and others) from sibling modules.
   - pyright flags `jobs/job.py` `failed + 1`, where `session.scalar` can return None. pyright is not in `make lint`.
   - `frontend/src/pages/channel/archive-section.tsx` is 450 lines.
   - ffmpeg is about 180-210 MB of the image. A smaller one means a static 8.x or 9.x build or a slim build from source, both with fewer codecs. Needs the user's approval.
6. Safari playback is out of scope until the user asks. The plan then: a cached mp4 remux with AAC audio, chosen by `canPlayType('video/x-matroska')`.
7. Review live recording with the user before building the recorder path. The user requires a monitoring view with it: which live each recorder job records now, which lives are scheduled to record, and the state of the recorder and worker pods.
8. Later: i18n with Korean added beside English.

## Suggested skills

`tdd` for new seams, `gh-cli` for workflow runs.

## Pointers

- `docs/plan/README.md`: design index, then only the topic files the task touches
- `docs/plan/archive.md`: dispatch, limits, pausing, retries, format fallback, audio copy, resume and integrity, cleanup
- `docs/plan/frontend.md`: pages, video page, settings, API
- homelab-workloads `savetube/appset.yaml` (`chartRevision`), `savetube/envs/prod.yaml` (`image.tag`), `savetube/manifests/prod/volumes.yaml` (archive PV, `local`, pinned to `talos-pve1`)
