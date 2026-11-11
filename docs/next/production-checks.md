# Production checks

## Goal

Confirm in production the released format fallback, recorder, and HTTP 403 changes.

## Next steps

1. No production job has reached `job.done` after a format fallback (unverified). When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count. Job 4429 `oT5iycnnqQs` fell back from 399 to 303 on HTTP 500, then failed on the members-only HTTP 403 that 0.32.0 fixes, so it does not count.
2. Production has not hit the recorder's give-up path (unverified). When a waiting room stays upcoming 24 hours past its start, check that the recorder logs `record.not_started`, its row shows `missed`, and its Job ends `succeeded` with no retry pod.
3. The `main` queue behind the 403 client fallback drains one job at a time, about 3-13 minutes each. Jobs 4710-4713 each logged `download.client_fallback clients=mweb`, then `job.done`, and `dispatch.profile_unblocked` cleared the `main` pause. Check in Loki that the remaining `main` jobs from 4714 on reach `job.done`, and that no `dispatch.profile_blocked` follows a `download.client_fallback`. The scheduler's `queue.summary` shows `profiles_blocked=none blocked=none`, so the header badge has no data to show. The user checks that it is gone in the production UI.
