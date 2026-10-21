# Next

## Goal

Ship what develop holds since `v0.7.0` when the user approves, then in-browser playback.

## Current progress

- Running in `savetube-prod`: `v0.7.0`, chart `v0.7.0`, migrations `0001`-`0011`. A merge to develop deploys nothing. A merge of develop into main releases and deploys (`docs/plan/operations.md` Versioning).
- The general cookie profile is `burner`, `max_concurrent` is 1. No bot check has returned since 18:39 KST on 2026-10-21.
- Production still fails a download on every dropped googlevideo connection, because `v0.7.0` passes no `retries` to yt-dlp. As a library, yt-dlp then retries 0 times. Develop fixes it (PR #18). The user chose to clear follow-ups before deploying.
- Develop holds, not deployed (PRs #17-#25):
  - yt-dlp retries: `retries` 10, `fragment_retries` 10, `extractor_retries` 3, `file_access_retries` 3. A local test server that stops halfway reproduces the production error without them.
  - Network interrupts count toward the cap of 5 only when a run added no bytes, so a download that moves on is retried until it ends.
  - Migration `0012` sets `failed` videos with a queued archive job back to `pending`, and the poll now does that when it queues.
  - Run ownership: a run whose job a reclaim took stops and writes nothing (`job.disowned`). The reclaim waits 300 s without a heartbeat.
  - Title-word narrowing cancels a queued job unless another archiving source still wants the video. A resumed run skips a finished chat replay. `job.progress` clears to SQL NULL.
  - UI: header pause refetch, `GET /api/jobs/counts`, the other-channels choice in the add-channel summary and before the first playlist, per-channel statistics by the channel list rule.
  - Image 1.22 GB to 1.17 GB, entry chunk 1,001 kB to 376 kB through route `lazy`. Route loading is not clicked through in a browser.
  - Tests: each run creates and drops `savetube_test_<random>`, and the health test no longer leaks a transport.
- Verified in production on `v0.7.0`: resume from `.part` files after a deploy, and `size`, `probe`, `packets` on the resumed file.
  - Channel library layout (PR #25), the variant the user picked from a three-variant prototype: sidebar channel list with filter, channel header row, tabs with counts, Playlists through the new `source_id` filter on `GET /api/videos`, no global search. `docs/plan/frontend.md` holds the rules.
- `savetube_proto` on pg-dev holds seeded demo data. `/tmp/savetube-library/serve.sh` serves a production build of a worktree against it, login skipped. Drop the database when no longer needed.
- CULUA is the one production channel.

## What worked

- DB tests: `bash /tmp/savetube-testdb/run-tests.sh <repo root> make check`. The script reads `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` and the password from `op read 'op://Homelab/savetube-dev-db/password'`, URL-quoted, with `sslrootcert` `~/.postgresql/postgres-dev-ca.crt`. It prints no secret. Parallel runs are safe.
- Parallel agents: make the worktrees yourself with `git worktree add -b <branch> ../savetube-wt-<name> develop`, give each a file boundary, and merge one PR per worktree.
- Production reads: `kubectl --context homelab-tailnet -n postgres exec postgres-2 -c postgres -- psql -d savetube -c "set default_transaction_read_only=on;" -c "<one query>"`. One query per call passes the permission classifier.
- Local UI checks: Playwright through `uvx --with playwright python -I <script>` with `channel="chrome"`. Frontend uses pnpm.
- yt-dlp with browser cookies on this Mac needs `--js-runtimes node`.

## What did not work

- The harness `isolation: worktree` starts from an old main merge commit, not develop.
- A background loop polling production with kubectl is denied by the permission classifier as "Production Deploy".
- `bsk` on `savetube.lab.hiriri.com` fails: the page holds another extension's frame. The user checks the production UI.
- An Argo CD refresh annotation is denied by the permission classifier. The webhooks make it unnecessary.
- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`.
- `make talos-config` fails off the home LAN. Use `talosctl patch mc --mode no-reboot` over the tailnet.

## Open questions

- Members-only videos get a Members tab only while the channel has some. The user has not confirmed this.
- Whether googlevideo still drops connections once yt-dlp retries in place.

## Next steps

1. Deploy develop when the user approves: merge develop into main, then check that 0012 ran, the 105 videos turned `pending`, and the worker logs yt-dlp "Retrying" lines instead of job errors.
2. The user checks the dashboard Downloads card and the header pause switch in production.
3. The user checks the channel library layout after the deploy.
4. Sources whose first poll has not run show nowhere since the Channels page went away. Show them, for example in the sidebar or on Home.
5. Follow-ups:
   - The worker logs a procrastinate error when it finishes a queue job a reclaim already failed.
   - The channel page "Queued (N)" still counts from a list capped at 500. Use `GET /api/jobs/counts?channel_id=`.
   - A killed test run leaves its `savetube_test_<random>` database behind.
   - The `/` route loads recharts on first paint. Lazy-load the chart.
   - ffmpeg is about 180-210 MB of the image. A smaller one means a static 8.x or 9.x build or a slim build from source, both with fewer codecs. Needs the user's approval.
6. In-browser playback, which the user approved: mount `tank/youtube` read-only in the web pod, serve media with HTTP Range, add a `<video>` element on the video page. Safari needs mp4 or WebM.
7. Review live recording with the user before building the recorder path. The user requires a monitoring view with it: which live each recorder job records now, which lives are scheduled to record, and the state of the recorder and worker pods.
8. Later: i18n with Korean added beside English.

## Suggested skills

`tdd` for new seams, `gh-cli` for workflow runs.

## Pointers

- `docs/plan/README.md`: design index, then only the topic files the task touches
- `docs/plan/archive.md`: dispatch, pausing, retries, ownership, resume and integrity, cleanup
- `docs/plan/frontend.md`: pages, dashboard, API
- homelab-workloads `savetube/appset.yaml` (`chartRevision`) and `savetube/envs/prod.yaml` (`image.tag`)
