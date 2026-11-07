# Next

## Goal

Confirm in production the released format fallback, recorder, and HTTP 403 changes.

## Next steps

1. Production has never run the format fallback (unverified). When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
2. Production has not run the recorder changes yet (unverified). After the next recorded live ends, check that the poll queues no archive job for its video while the record job runs, and that the worker log holds no `FileExistsError` for it. When a waiting room stays upcoming 24 hours past its start, check that the recorder logs `record.not_started`, its row shows `missed`, and its Job ends `succeeded` with no retry pod.
3. The 403 client fallback (release 0.32.0) downloaded its first members-only video with `mweb` on 2026-11-07 18:32 KST, job 4710 `7_P2Drj692k`. 109 `main` jobs waited behind it. Check in Loki that they reach `job.done`, that no `dispatch.profile_blocked` follows a `download.client_fallback`, and that the header badge leaves once no pause holds.
