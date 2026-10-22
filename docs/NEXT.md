# Next

## Goal

`v0.12.1` is confirmed in production, except the format fallback, which production has never run.

## Next steps

1. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
