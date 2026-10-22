# Next

## Goal

Confirm `v0.8.0` in production, then in-browser playback.

## Current progress

- Running in `savetube-prod`: `v0.8.0`, chart `v0.8.0`, migrations `0001`-`0012`, deployed 23:08 KST on 2026-10-21 through PR #33. A merge to develop deploys nothing. A merge of develop into main releases and deploys (`docs/plan/operations.md` Versioning).
- The general cookie profile is `burner`, `max_concurrent` is 1. No bot check has returned since 18:39 KST on 2026-10-21.
- Checked after the deploy: no `failed` video has a queued archive job, and the first job resumed from its `.part` file. No dropped connection had happened yet, so yt-dlp "Retrying" lines are not seen.
- `v0.8.0` added (PRs #17-#32):
  - yt-dlp retries: `retries` 10, `fragment_retries` 10, `extractor_retries` 3, `file_access_retries` 3. A local test server that stops halfway reproduces the production error without them.
  - Network interrupts count toward the cap of 5 only when a run added no bytes, so a download that moves on is retried until it ends.
  - Migration `0012` sets `failed` videos with a queued archive job back to `pending`, and the poll now does that when it queues.
  - Run ownership: a run whose job a reclaim took stops and writes nothing (`job.disowned`). The reclaim waits 300 s without a heartbeat.
  - Title-word narrowing cancels a queued job unless another archiving source still wants the video. A resumed run skips a finished chat replay. `job.progress` clears to SQL NULL.
  - UI: header pause refetch, `GET /api/jobs/counts`, the other-channels choice in the add-channel summary and before the first playlist, per-channel statistics by the channel list rule.
  - Image 1.22 GB to 1.17 GB, entry chunk 1,001 kB to 376 kB through route `lazy`. Route loading is not clicked through in a browser.
  - Tests: each run creates and drops `savetube_test_<unix seconds>_<hex>` and drops such databases older than 24 hours. The health test no longer leaks a transport.
  - The worker ends a queue job the reclaim already failed without a procrastinate error.
  - The channel page "Queued (N)" counts from `GET /api/jobs/counts`. The Home chart and recharts load lazily: dashboard chunk 382.75 kB to 57.30 kB.
  - Home shows a "Sources waiting" card while a source has no channel: before its first poll, after a failed first poll, or disabled.
  - Channel library layout (PR #25), the variant the user picked from a three-variant prototype: sidebar channel list with filter, channel header row, tabs with counts, Playlists through the new `source_id` filter on `GET /api/videos`, no global search. `docs/plan/frontend.md` holds the rules.
- Verified in production on `v0.7.0`: resume from `.part` files after a deploy, and `size`, `probe`, `packets` on the resumed file.
- `savetube_proto` on pg-dev holds seeded demo data. `/tmp/savetube-library/serve.sh` serves a production build of a worktree against it, login skipped. Drop the database when no longer needed.
- CULUA is the one production channel.

## What worked

- DB tests: `bash /tmp/savetube-testdb/run-tests.sh <repo root> make check`. The script reads `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` and the password from `op read 'op://Homelab/savetube-dev-db/password'`, URL-quoted, with `sslrootcert` `~/.postgresql/postgres-dev-ca.crt`. It prints no secret. Parallel runs are safe.
- Parallel agents: make the worktrees yourself with `git worktree add -b <branch> ../savetube-wt-<name> develop` and give each a file boundary. `CLAUDE.md` Merging says how a branch lands.
- `.claude/settings.local.json` allows `gh pr merge`, `git rebase`, `git merge`, `git pull`, and `git push origin develop`. The auto mode classifier otherwise blocks them as "Merge Without Review", and it blocked `git pull --ff-only` and a read-only `gh pr view` loop right after a merge.
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

1. Check the worker logs for yt-dlp "Retrying" lines instead of job errors once a googlevideo connection drops.
2. The user checks the dashboard Downloads card and the header pause switch in production.
3. The user checks the channel library layout after the deploy.
4. The user checks the "Sources waiting" card after the deploy. Its "Poll now" button is not clicked in any test.
5. Follow-ups:
   - The channel page hides "Show more" once all 500 listed queued jobs show, even when more wait.
   - `GET /api/sources` sends `last_error` raw. The Home card only cuts it at 120 characters, without the yt-dlp noise stripping `job_reason` does.
   - The worker swallows the finish error for any queue row already `failed`. Only the reclaim fails one today.
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
