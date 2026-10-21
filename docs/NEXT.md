# Next

## Goal

Get production downloading again after YouTube's bot check blocked it, then build in-browser playback.

## Current progress

- **No archive download has succeeded in production since 2026-10-21 13:48 KST.** YouTube's bot check ("Sign in to confirm you're not a bot") started failing jobs at 13:44. The same check hit local e2e runs from this Mac at about 13:40, so the home egress IP is the likely target. Unverified.
- Until about 18:40 KST the channel-level pause (source `archiving` off) held CULUA's downloads. The user then turned it off, picked `burner` as the general cookie profile, and set `max_concurrent` 1. At 18:42 one archive job ran and 115 waited.
- Archive jobs at 18:38 KST:

  | state | count |
  |---|---|
  | succeeded | 128 |
  | interrupted | 111 |
  | failed | 31 |
  | cancelled | 14 |

- Running in `savetube-prod`: `v0.6.1`, chart `v0.6.1`. Migrations `0001`-`0010` applied. The deploy job moves `image.tag` and `chartRevision` together (`docs/plan/operations.md` Versioning).
- Released `v0.6.0`:
  - Resume and integrity: interrupted downloads keep their parts and resume, SIGTERM marks jobs interrupted, files pass size, ffprobe and `ffmpeg -c copy -f null` checks. Migration `0008`. Design in `docs/plan/archive.md` "Resuming and verifying downloads".
  - YouTube-block pause: a bot check or HTTP 429 makes the job interrupted with no attempt used, and pauses all archive dispatch for 30 min, doubling to 12 h. Migration `0009` gave back the attempts earlier bot-check failures used.
  - Cookie profiles: each `<name>.txt` key of Secret `savetube-cookies` is a profile. A general profile in Settings serves polls and non-members downloads, and members-only content uses a profile confirmed as a member. Migration `0010` dropped `channel.use_cookies`.
  - Channel page job lists show 10 and grow by 10.
- Resume has not run on a real production download, and ffprobe and ffmpeg checks have never run on a real file.
- CULUA is the one production channel: Videos and Live tabs, 5 playlists, words `歌枠`, `歌ってみた`, `Official Music Video`. Its 5 playlists still have other-channels off unless the user turned the checkbox on.
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

- Which mitigation clears the bot check if `burner` does not: the bgutil PO token (does a token reach a request at all?), `curl-cffi` impersonation, lower concurrency with `-t sleep` intervals, or waiting it out? `docs/plan/background.md` notes the ban risk for a logged-in account.
- An intermittent `PytestUnraisableExceptionWarning` (an asyncio server transport collected late, probably the health server in a test) shows in about 1 of 6 runs.
- Chat replay downloads again on every resume.
- If procrastinate marks a live worker stalled, two downloads can share `.incoming/<video id>/`.
- The image is 1.22 GB. The frontend bundle is 993 kB.

## Next steps

1. Bot check, before anything else:
   1. Check whether downloads through `burner` at concurrency 1 pass the bot check, and whether a block logs `dispatch.blocked` and pauses dispatch.
   2. Pick a further mitigation with the user if the block returns. The open question above lists them.
2. Downloads control and status, which the user asked for. Production will hold many channels, so one channel's page is the wrong place for either:
   - A global pause in the header or the sidebar. Today the only pause is per channel (source `archiving`, `frontend/src/pages/channel.tsx`). Ask the user whether the channel-level pause stays, and whether a global pause also stops running downloads or only new dispatch.
   - The dashboard shows what downloads now: the channel and the video of each running job, with its progress.
3. On the first real downloads, check that ffprobe and ffmpeg run in the worker pod, and that a later deploy resumes a running download.
4. In-browser playback, which the user approved: mount `tank/youtube` read-only in the web pod, serve media with HTTP Range, add a `<video>` element on the video page. Chrome and Firefox play the stored mkv with VP9 or AV1 and Opus. Safari needs mp4 or WebM, so store WebM when the codecs allow or remux on the fly.
5. Follow-ups:
   - The add-channel step 4 summary omits the other-channels choice.
   - The channel page has no way to set other-channels before a playlist exists.
   - `JobOut` omits `log_tail`, so interrupted and failed reasons show only in the database.
   - The dashboard's per-channel statistics count by uploader and include uploader-only channels.
   - Narrowing words does not cancel a queued job whose video another source also lists.
6. Review live recording with the user before building the recorder path.

## Suggested skills

`diagnosing-bugs` for the bot check, `tdd` for new seams, `gh-cli` for workflow runs.

## Pointers

- `docs/plan/README.md`: design index, then only the topic files the task touches
- `docs/plan/operations.md` Logging: the logfmt event policy
- `docs/plan/archive.md`: dispatch, members-only rule, resume and integrity
- homelab-workloads `savetube/appset.yaml` (`chartRevision`) and `savetube/envs/prod.yaml` (`image.tag`)
