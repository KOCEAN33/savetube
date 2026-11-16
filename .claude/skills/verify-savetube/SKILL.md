---
name: verify-savetube
description: Drives the savetube web UI as a user against a local seeded instance and keeps the evidence. Use when a savetube change to a page, an /api route, or a job list needs proof in the running app, or when a claim about what the UI does needs checking.
---

# verify-savetube

Goal: prove one user-facing behavior of savetube in a running instance, with artifacts that outlive the instance. The surface is the web UI. The instance is the web process alone, with login skipped, over a database that `e2e/seed.py` seeds. No worker and no scheduler run, so a queued job stays queued. Worker behavior is proven by `backend/tests/test_e2e.py`.

Run every command from the repo root. Each Bash call is a new shell: set `RUN` and source `instance.env` again in each one.

## Launch

```sh
RUN=/tmp/savetube-verify/$(date +%Y%m%d-%H%M%S)
scripts/test-db.sh e2e/instance.sh up "$RUN"
. "$RUN/instance.env"   # base, port, pid, db, build
```

`up` builds the frontend, creates and seeds a database on the dev server, and serves on a free port. It returns after `== ready <base>`, when the API answers from the database. Each run directory is its own instance, so two runs can stand side by side. A failed `up` removes what it made.

## Doctor

```sh
e2e/instance.sh doctor "$RUN"
```

Read-only. It prints one `ok` or `FAIL` line per check and exits 1 on any `FAIL`: the server is the process `up` started, it holds the port, login is skipped, the database answers, and the build matches the checkout. Run it before the first drive and whenever a result looks off. A `FAIL` on the build line means the code changed after `up`: run `down` and `up` again, because the server never reloads.

## Drive

`features/README.md` indexes the feature map: the seeded data, the conventions, and one recipe file per feature. Read it, then follow the recipe of the feature under proof. A feature with no file gets one in the same change, in the shape the README states.

`pw` in a recipe stands for the helper below. It runs one `playwright-cli` command in the run's own browser session, from `$RUN/evidence`.

```sh
.claude/skills/verify-savetube/scripts/pw "$RUN" open "$base/" --browser=chrome
.claude/skills/verify-savetube/scripts/pw "$RUN" snapshot "role=main"
```

A target is a Playwright selector: `role=button[name='Retry']`, an id, or `a >> b` to scope. Refs such as `e250` change between runs, so recipes never hold one.

## Evidence

Artifacts go to `$RUN/evidence/<feature>/`, numbered in the order of the journey. A proof holds:

- the state before the action and the state after it, as `pw snapshot --filename=<feature>/<n>-<name>.yml`
- the request the action sent, from `pw requests --filter=<path regexp>`
- for a mutation, a second view of the stored value: another page that shows it, and the `/api` read saved as JSON
- one full-page screenshot of the end state
- `pw console error` with no error the feature file does not name

Drive the path a user takes: links, buttons and fields. A `curl` to `/api` reads a result and never performs the action under proof. The report names the feature id, the entry point used, and each entry point not driven.

## Cleanup

```sh
.claude/skills/verify-savetube/scripts/pw "$RUN" close
scripts/test-db.sh e2e/instance.sh down "$RUN"
ls "$RUN/evidence"
```

`down` stops only the server `up` started, drops the database, and removes `$RUN/work` and `$RUN/instance.env`. `$RUN/evidence` stays. Run both commands after a failed drive too. `/tmp` does not survive a reboot: copy the evidence when the task names a place for it.

## Gotchas

- A mutation uses up its seed row: the one retryable job has no `Retry` button after one click. Start a fresh instance to drive the recipe again.
- `pw uncheck` and `pw check` on a Radix checkbox or switch report "did not change its state" although the click landed. Use `pw click` and read the state back.
- Seeded video ids do not exist on YouTube, so a page that shows a thumbnail logs one console error for `i.ytimg.com` with status 404. Any other console error counts.
- `pw requests` hides media and asset requests. Add `--static` to see `/api/videos/<id>/media`.
