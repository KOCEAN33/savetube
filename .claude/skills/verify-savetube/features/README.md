# savetube verification map

The maintained source for proving the user-facing behavior of savetube. Read this index, then use the matching feature file as the recipe.

## Baseline

- The instance comes from `e2e/instance.sh up "$RUN"` and passes `e2e/instance.sh doctor "$RUN"`. Never drive an instance this run did not start.
- `$base` comes from `. "$RUN/instance.env"`.
- `pw` stands for `.claude/skills/verify-savetube/scripts/pw "$RUN"`. The first command of a session is `pw open "$base/" --browser=chrome`.
- Every recipe starts from a fresh seed unless its preconditions say otherwise.

## Seed

`e2e/seed.py` is the source. What the recipes rely on:

| Row | Value |
|---|---|
| Channels | `E2E Main` (`UCe2eMainChannel000000001`), `E2E Other` (`UCe2eOtherChannel00000002`) |
| Playable video | `e2ePlay0001`, "Playable smoke video", 2 s H.264 with AAC, a real file with audio copy and English subtitles |
| Running download | `e2eRun00001`, "Downloading upload", 42 % |
| Queued jobs | archive of `e2ePend0001` "Waiting upload", replace of `e2eVideo002` "Second upload" with format 299 |
| Failed jobs | `e2eFail0001` "Broken upload", 3 attempts, retryable. `e2eGone0001` "Removed upload", video state `gone`, counts as resolved |
| Download settings | max concurrent 2, rate limit 5 MB/s, daily limit 20, window off |
| Format watch | on `e2ePlay0001`: stored 137 and 140, offered 251 (Opus) |

## Driving conventions

- Target by role and accessible name first, then by id. Never by ref, position, or copy text outside a name.
- Treat every command as literal. Keep quoted names and flags unchanged.
- After each action, read the state the step names before the next action. A step whose state does not appear is a failed journey: report it, and put the reason in that feature's `Gotchas`. Never weaken the step.

## Proof and skip reporting

- `SKILL.md` § Evidence lists what a proof holds and where it goes.
- Record the feature id and the entry point with every artifact name or report line.
- Report an unreachable path with the command tried and the precondition that was missing.
- An entry point not driven is reported as not driven, never as covered by another path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph on the user-visible behavior, then exactly four H2 sections in this order.

1. `Sub-features`: short ids, one line per behavior.
2. `How to get to it (user POV)`: every user entry point.
3. `Driving it with pw`: `Preconditions:` first, then labeled bullets that pair a user action with its exact command and the observable result.
4. `Gotchas`: traps that waste or invalidate a run.

The map holds user paths, stable handles, required state, commands, and observable proof. Implementation detail stays in the code.

## Features

- [Downloads](./downloads.md): the running download, the queue, the finished list with its resolved and state filters, and retry of a failed job.
- [Videos](./videos.md): the video list, its channel and codec filters, and opening a video from a row.
- [Video page](./video.md): playback of a stored video and the format watch card with its replace pick.
- [Settings](./settings.md): the download limits form.

Pages with no recipe: Home (`/`), Live (`/live`), Channel (`/channels/<id>`), Encode (`/encode`). `e2e/smoke.py` checks that each renders.
