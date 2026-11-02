# Next

## Goal

Build the members-only onboarding, then confirm the production paths that have not run yet.

## Next steps

1. Onboarding a channel whose uploads are all members-only. The user is a member, but the add-channel scan offers no Videos or Shorts tab, because `POST /api/channel-scan` lists without cookies and sees no entries. With no tab chosen, no poll lists a members-only entry, so the membership check never runs either. The user wants the onboarding to check membership and list the tabs with the member profile. The web pod never mounts the cookie Secret (`docs/plan/access.md`, Cookies and account), so the scheduler does the member listing and the check.
   - Decision: a user action that asks the scheduler to act now goes over HTTP, never through a DB flag the scheduler loop picks up. The procrastinate queue stays in Postgres.
   - Build: add the routes to the scheduler's internal API, `backend/savetube/scheduler_api.py` behind the ClusterIP Service `savetube-scheduler` (`docs/plan/runtime.md`, Scheduler API).
   - The onboarding call: `POST /api/channel-scan` calls the scheduler, which lists the tabs with the cookie profiles, checks membership on a members-only entry, stores `membership`, and answers within the scan's 120 s timeout. This lists with the real account, which `docs/plan/access.md` keeps for members-only downloads today, so that section changes.
   - Update `docs/plan/access.md`, `docs/plan/frontend.md` (Add channel) and `docs/plan/operations.md` with the API.
2. Production has never run the format fallback. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
3. Poll now has not run in production since `v0.21.0` moved it to the scheduler's API. After the user presses Poll now on a channel source, check that the scheduler logs `poll.requested` within seconds and the web log holds no `source.poll_request_failed`.
4. `/tmp/savetube-testdb/run-tests.sh` and `/tmp/savetube-library/serve.sh`, which `docs/guides/testing.md` names, were lost when /tmp was cleared. Recreate them before the next DB test run. The run-tests script reads the dev DB password through 1Password, so the user approves that read.
5. Production has not run the recorder changes yet. After the next recorded live ends, check that the poll queues no archive job for its video while the record job runs, and that the worker log holds no `FileExistsError` for it. When a waiting room stays upcoming 24 hours past its start, check that the recorder logs `record.not_started`, its row shows `missed`, and its Job ends `succeeded` with no retry pod.
