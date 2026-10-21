# Next

## Goal

Get production downloading again after YouTube's bot check blocked it, ship the resume and integrity work already on `develop`, then build in-browser playback.

## Current progress

- **Production is blocked by YouTube's bot check since 2026-10-21 13:44 KST.** Archive jobs fail in 2-3 s with "Sign in to confirm you're not a bot" (111 log lines in 30 min). In those 30 min, 20 jobs succeeded and 113 failed. The same check hit local e2e runs from this Mac at about 13:40, so the home egress IP is the likely target. Likely triggers: max_concurrent 5 in production plus local e2e traffic. Unverified.
- Each bot-check failure counts as an attempt (`MAX_ARCHIVE_ATTEMPTS` 3). At the last check, 92 videos had 1 failed attempt and 25 had 2. A third failure gives a video up for good.
- The user was asked to close the download window in Settings (for example 03:00-03:01) to stop dispatch. Whether they did is unchecked.
- Running in `savetube-prod`: `v0.5.0`. Migrations `0001`-`0007` applied. Chart pinned at `chartRevision: v0.4.0` in homelab-workloads `savetube/appset.yaml`.
- `develop` is 2 commits ahead of main and pushed, not released:
  - `258eaf7` resume and integrity: interrupted downloads become `interrupted` rows that attempt counting ignores, partial files stay in `.incoming/<video id>/` and resume with the pinned format, SIGTERM marks jobs interrupted, integrity checks (exact filesize, ffprobe duration and streams, `ffmpeg -c copy -f null` after a resume). Migration `0008`. Chart change: worker `terminationGracePeriodSeconds: 30`. Design in `docs/plan/archive.md` "Resuming and verifying downloads".
  - `8d67370` the channel page's queued and finished job lists show 10 and grow by 10.
- Resume was shown locally: SIGTERM at 351,516 bytes, the next worker resumed from there with attempts unchanged (`/tmp/savetube-shots/resume-log.txt`, gone after reboot). A resumed download reaching `download.verified` was not shown, because the bot check started. ffprobe and ffmpeg checks have never run on a real file: this Mac has neither.
- Released this session: `v0.3.0` per-channel settings and the add-channel modal, `v0.3.1` scheduler dispatch (no procrastinate row per waiting job) and members-only skip without cookies, `v0.4.0` logfmt logging, channel avatar, download progress, channel videos, `v0.5.0` playlist videos from other channels (checkbox, default on for new channels) and `tracked` channels.
- CULUA is the one production channel: Videos and Live tabs, 5 playlists, words `歌枠`, `歌ってみた`, `Official Music Video`, cookies off. Its 5 playlists still have other-channels off unless the user turned the checkbox on.
- Chart pin rule: a release moves only `image.tag`. When a release changes `charts/`, raise `chartRevision` in homelab-workloads after the new image runs. `v0.5.0` to the next release changes the chart.
- Every deploy so far killed running downloads. `v0.5.0` and older count them as failed attempts. The resume release fixes that from its own deploy on, but the deploy that ships it still kills the old workers' downloads the old way.
- Argo CD webhooks: savetube, homelab-workloads and homelab-platform push to `https://argocd.hiriri.com/api/webhook`. The secret is field `github-secret` of the 1Password item `argocd-webhook` in the Homelab vault.
- Deploy pipeline, Zitadel, Postgres, storage: unchanged, see `docs/plan/operations.md` and homelab-workloads `savetube/`.
- The user's design decisions are rules in `docs/plan/`. This session's: no separate rule concept, settings live on the channel. One modal adds a channel. Sidebar Dashboard, Channels, Live, Settings, Videos. Centered modals, never side sheets. Playlist videos from other channels download once under the uploader's folder.

## What worked

- DB tests against pg-dev: export `SAVETUBE_TEST_DATABASE_URL` in a subshell, with the password from `op read 'op://Homelab/savetube-dev-db/password'`. Take `OP_SERVICE_ACCOUNT_TOKEN` from the keychain item `proxmox-talos-lab-op` first. URL-quote the password, and point `sslrootcert` at `~/.postgresql/postgres-dev-ca.crt`.
- Local end-to-end check: a throwaway database `savetube_e2e` on pg-dev (exists, sources disabled), `SAVETUBE_ARCHIVE_ROOT` under `/tmp`, `SAVETUBE_STATIC_DIR=frontend/dist`, and a uvicorn script that sets `app.dependency_overrides[current_user]` to skip login. Run `savetube scheduler` and `savetube worker` beside it, each with its own `SAVETUBE_METRICS_PORT` and `SAVETUBE_HEALTH_PORT`. macmon holds 9090 on this Mac. Use the channel quality `wa[ext=m4a]/wa`, because this Mac has no ffmpeg.
- Production reads without the UI: `kubectl --context homelab-tailnet -n postgres exec postgres-2 -c postgres -- psql -d savetube -c "set default_transaction_read_only=on; ..."`. Writes through this path are denied by the permission classifier. Use the UI's buttons, such as Poll now.
- Screenshots: Playwright through `uvx --with playwright python -I <script>` with `channel="chrome"`.
- yt-dlp with browser cookies on this Mac needs `--js-runtimes node`, because Deno is not installed.
- Parallel agents: give file-disjoint work to separate agents, or use worktrees. Two agents on `jobs.py`, `api.py` or the channel page collide, and two new migrations collide on the number.

## What did not work

- An Argo CD refresh annotation (`kubectl annotate application`) is denied by the permission classifier. The webhooks make it unnecessary.
- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`. `tests/test_auth.py` patches `httpx2.AsyncHTTPTransport`.
- `make talos-config` fails off the home LAN. Use `talosctl patch mc --mode no-reboot` over the tailnet.

## Open questions

- Which mitigation clears the bot check: cookies from a separate account, the bgutil PO token (does a token reach a request at all?), `curl-cffi` impersonation, lower concurrency with `-t sleep` intervals, or waiting it out? `docs/plan/background.md` notes the ban risk for a logged-in account.
- An intermittent `PytestUnraisableExceptionWarning` (an asyncio server transport collected late, probably the health server in a test) shows in about 1 of 6 runs.
- Chat replay downloads again on every resume.
- If procrastinate marks a live worker stalled, two downloads can share `.incoming/<video id>/`.
- The image is 1.22 GB. The frontend bundle is 993 kB.

## Next steps

1. Bot check, before anything else:
   1. Read production logs and the DB to confirm downloads stopped and whether the bot check still answers.
   2. Treat "Sign in to confirm you're not a bot" (and similar blocks) as not an attempt, and pause all dispatch with a growing backoff while it lasts. Log it as `dispatch.blocked`.
   3. Give back the attempts the bot check used: failed archive jobs whose `log_tail` holds the bot text become `interrupted` (a migration or a one-off script the user runs).
   4. Pick a mitigation with the user. The open question above lists them.
2. Release `develop` (resume, integrity, job paging): PR to main, merge, watch the rollout and migration `0008`, then raise `chartRevision` to the new tag. Check ffprobe and ffmpeg run on a real file in the worker pod, and that a later deploy resumes a running download.
3. In-browser playback, which the user approved: mount `tank/youtube` read-only in the web pod, serve media with HTTP Range, add a `<video>` element on the video page. Chrome and Firefox play the stored mkv with VP9 or AV1 and Opus. Safari needs mp4 or WebM, so store WebM when the codecs allow or remux on the fly.
4. Follow-ups:
   - The add-channel step 4 summary omits the other-channels choice.
   - The channel page has no way to set other-channels before a playlist exists.
   - `JobOut` omits `log_tail`, so interrupted and failed reasons show only in the database.
   - The dashboard's per-channel statistics count by uploader and include uploader-only channels.
   - Narrowing words does not cancel a queued job whose video another source also lists.
5. Review live recording with the user before building the recorder path.

## Suggested skills

`diagnosing-bugs` for the bot check, `tdd` for new seams, `gh-cli` for workflow runs.

## Pointers

- `docs/plan/README.md`: design index, then only the topic files the task touches
- `docs/plan/operations.md` Logging: the logfmt event policy
- `docs/plan/archive.md`: dispatch, members-only rule, resume and integrity
- homelab-workloads `savetube/appset.yaml` (`chartRevision`) and `savetube/envs/prod.yaml` (`image.tag`)
