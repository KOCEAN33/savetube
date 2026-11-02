# Next

## Goal

Confirm the members-only onboarding and the production paths that have not run yet.

## Next steps

1. The add-channel scan runs in the scheduler (`POST /channel-scan`, `docs/plan/runtime.md`, Scheduler API) and lists with every cookie profile. After its release, the user scans the channel whose uploads are all members-only. Check these things:
   - The scan answers within 120 s, offers the Videos tab, and the scan result shows a members-only badge for `main` with "member". The scheduler log shows `membership.checked` for each profile and no `scan.profile_failed`, and the web log holds no `scan.request_failed`.
   - After the user adds the channel with the Videos tab, the first poll lists with the general profile, finds no entries, and lists again with the member profile. Check that the scheduler log shows `poll.member_listing` with `profile=main` for the source, then `poll.done` with the members-only entries in `listed` and `queued`, and no `skip_members`. A `poll.failed` with "does not have a videos tab" and no `poll.member_listing` means the member listing did not run: check the channel's membership result on the channel page and `blocked_until`.
2. Production has never run the format fallback. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
3. Poll now has not run in production since `v0.21.0` moved it to the scheduler's API. After the user presses Poll now on a channel source, check that the scheduler logs `poll.requested` within seconds and the web log holds no `source.poll_request_failed`.
4. `/tmp/savetube-testdb/run-tests.sh` and `/tmp/savetube-library/serve.sh`, which `docs/guides/testing.md` names, were lost when /tmp was cleared. Recreate them before the next DB test run. The run-tests script reads the dev DB password through 1Password, so the user approves that read.
5. Production has not run the recorder changes yet. After the next recorded live ends, check that the poll queues no archive job for its video while the record job runs, and that the worker log holds no `FileExistsError` for it. When a waiting room stays upcoming 24 hours past its start, check that the recorder logs `record.not_started`, its row shows `missed`, and its Job ends `succeeded` with no retry pod.
