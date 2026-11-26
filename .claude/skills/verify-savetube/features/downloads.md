# Downloads

The Downloads page shows the download running now, the queue in start order, and the finished jobs. A user hides or shows resolved failures, filters the finished jobs by state, and sends one failed job, or every retryable one at once, back to the queue.

## Sub-features

- `dl-running` shows the running download with its progress line.
- `dl-queue` lists queued archive and replace jobs, next first.
- `dl-resolved` hides resolved failures by default and shows them on request.
- `dl-state` filters the finished jobs by state.
- `dl-retry` queues a failed job again.
- `dl-retry-all` queues every retryable failed job again and names how many it queued.

## How to get to it (user POV)

- Choose `Downloads` in the sidebar.
- Open `/downloads`, with `?resolved=show` and `&state=failed` holding the two filters.

## Driving it with pw

Preconditions:

- A fresh seed: "Broken upload" is failed and has not been retried.
- A browser session is open: `pw open "$base/" --browser=chrome`.

- **Sidebar entry.** Choose `Downloads`. Run `pw click "role=link[name='Downloads']"`. The page URL ends in `/downloads`.
- **Baseline.** Run `pw snapshot "role=main" --filename=downloads/1-before.yml`. The file holds `Downloading upload` with `42.0%`, the heading `Queued (2), next first`, the text `replace 299`, one `button "Retry"` in the "Broken upload" row, and no `Removed upload`.
- **Show resolved.** Clear `Hide resolved`. Run `pw click "#downloads-hide-resolved"`. The page URL holds `resolved=show`.
- **Filter by state.** Open `State` and choose `failed`. Run `pw click "role=combobox[name='State']"` and `pw click "role=option[name='failed']"`. The page URL holds `state=failed`.
- **Filtered state.** Run `pw snapshot "role=main" --filename=downloads/2-failed.yml`. The finished table holds the rows "Broken upload" and "Removed upload" only. The "Removed upload" row reads `gone` and has no `Retry` button.
- **Retry.** Choose `Retry`. Run `pw click "role=button[name='Retry']"`, then `pw requests --filter="/api/jobs/[0-9]+/retry" > "$RUN/evidence/downloads/3-retry-request.txt"`. The file holds one `[POST]` line with `[200]`.
- **After retry.** Run `pw snapshot "role=main" --filename=downloads/4-after-retry.yml`. The file holds no `button "Retry"` and the heading `Queued (3), next first`.
- **Second view.** Load the page again without filters. Run `pw goto "$base/downloads"` and `pw snapshot "role=main" --filename=downloads/5-queued.yml`. "Broken upload" is a row of the queued table, above the heading of the finished jobs.
- **Stored value.** Run `curl -fsS "$base/api/jobs?state=queued" > "$RUN/evidence/downloads/6-queued.json"`. One job has `"video_id": "e2eFail0001"`, `"kind": "archive"` and `"attempts": 0`.
- **Retry all.** On a fresh seed, choose `Retry all failed`. Run `pw click "role=button[name='Retry all failed']"`, then `pw requests --filter="/api/jobs/retry-failed"`. It holds one `[POST]` line with `[200]`. The finished section shows the status `1 queued`, and the heading reads `Queued (3), next first` with "Broken upload" in the queued table.
- **Proof.** Run `pw screenshot --full-page --filename=downloads/7-queued.png` and `pw console error > "$RUN/evidence/downloads/8-console.txt"`. The console file reads `Errors: 0`.

## Gotchas

- The retried job stays queued, because no worker runs. Proof that a retried download finishes belongs to `backend/tests/test_e2e.py`.
- After the retry the failed job is no longer its video's newest, so its row loses the `Retry` button. A second `Retry` click needs a fresh instance.
- `Dismiss` and `Pause downloads` have no recipe.
