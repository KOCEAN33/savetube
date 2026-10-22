# Next

## Goal

Confirm `v0.12.1` in production. The release changes no behavior, so the checks below cover features from `v0.9.0`-`v0.11.0` that production has not exercised yet.

## Next steps

1. Check the step label on the Downloads card through merging, checking the file and writing the audio copy (`docs/plan/frontend.md`).
2. When the queued job for `7l-RzDkD7a0` runs, check the worker log for `download.fallback` with 299, then 312, then 298+251, and then `job.done`. The fallback is in `docs/plan/archive.md`, Resuming and verifying downloads.
3. The user checks playback, the audio player and subtitles on `savetube.lab.hiriri.com` in Chrome. Include a multi-hour live and the ⋮ captions menu, which no test clicked. Only `pjiTdkZ9DoI` has `.vtt` files.
