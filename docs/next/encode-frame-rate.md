# Encode frame rate

## Goal

Game and 3D live streams keep 60 fps and still streams go to 30 fps, decided by measured motion, with savetube jobs decoded on NVDEC. Then the frame-lost videos that need 60 fps are downloaded again. Design, constants and done checks: `docs/plan/encode-frame-rate.md`.

## Start

Plan step 6, after one check: the first encode job queued with `motion_threshold` succeeds. Both releases run in production: media-encoder v0.3.15 on the PC, and savetube PR #75 with migration `0037` (`fps_cap` 30, `motion_threshold` 1.0). On 2026-11-11 the queue still held 3 jobs from before the release, so the first measured job queues after those.

- The owner chose to download again only the frame-lost videos that need 60 fps, picked by motion score, not all 210 (plan, Re-downloading frame-lost videos).

## Next steps

1. Check the first measured job: its `encode_job.options` has `motion_threshold`, its done file has `motion` and `fps`, and it succeeded.
2. Plan step 6. Done when every candidate at or above 1.0 has a succeeded replace.

## Pointers

- GPU tests run only on the PC: media-encoder `scripts/pc-test.sh ./internal/ffmpeg`. The PC's `claude` account cannot reach the `youtube` share. Cut sample clips in the savetube scheduler pod with `ffmpeg -c copy` and copy them over.
- Measurements and their scripts: llm-wiki `projects/savetube-encode-fps.md`.
