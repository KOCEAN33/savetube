# Encode frame rate

## Goal

Game and 3D live streams keep 60 fps and still streams go to 30 fps, decided by measured motion, with savetube jobs decoded on NVDEC. Then the frame-lost videos that need 60 fps are downloaded again. Design, constants and done checks: `docs/plan/encode-frame-rate.md`.

## Start

Plan step 6 is queued. On 2026-11-11 every candidate was scored: 106 of 210 scored at or above 1.0. Each got a manual replace of its 60 fps video format at the stored height, jobs 4915-5020. v0.39.0 makes such a replace mark the video's encode job `superseded`, so the video queues for encoding again once its 7-day original keep ends.

## Next steps

1. Check that the 106 manual replaces, jobs 4915-5020, succeeded.
2. Check the first encode queued with `motion_threshold`: its `encode_job.options` has `motion_threshold`, its done file has `motion` and `fps`, and it succeeded. A re-downloaded video queues from about 2026-11-18, after its original keep. A new download of a title that is not still queues sooner.

## Pointers

- Scoring results and measurements: llm-wiki `projects/savetube-encode-fps.md`.
- GPU tests run only on the PC: media-encoder `scripts/pc-test.sh ./internal/ffmpeg`. The PC's `claude` account cannot reach the `youtube` share. Cut sample clips in the savetube scheduler pod with `ffmpeg -c copy` and copy them over.
- A pod restart clears its `/tmp`. Long scoring runs belong in a standalone pod that mounts `savetube-archive` read-only.
