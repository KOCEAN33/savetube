# Next

## Goal

Confirm `v0.12.1` in production. The release changes no behavior, so the checks below cover features from `v0.9.0`-`v0.11.0` that production has not exercised yet.

## Next steps

1. The user checks the step label on the Downloads card through merging, checking the file and writing the audio copy (`docs/plan/frontend.md`). The API side is confirmed: running jobs report `merging` and `audio` in `job.progress`.
2. Production has never run the format fallback. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
3. The user checks playback and the audio player on `savetube.lab.hiriri.com` in Chrome, including a multi-hour live.
