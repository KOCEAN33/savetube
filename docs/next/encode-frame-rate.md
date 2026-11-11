# Encode frame rate

## Goal

Game and 3D live streams keep 60 fps and still streams go to 30 fps, decided by measured motion, with savetube jobs decoded on NVDEC. Then the frame-lost videos that need 60 fps are downloaded again. Design, constants and done checks: `docs/plan/encode-frame-rate.md`.

## Start

Plan step 5, second half: the savetube release (develop into main) waits for the owner's go. Plan steps 1-4 are on develop and media-encoder `main`, and media-encoder v0.3.15 is released. The PC installs v0.3.15 once its running jobs finish. Confirm the status page at `http://100.112.180.96:8733/` shows v0.3.15 before the savetube release.

- The owner chose to download again only the frame-lost videos that need 60 fps, picked by motion score, not all 210 (plan, Re-downloading frame-lost videos).

## Next steps

1. The savetube release, on the owner's go.
2. Plan step 6 after both releases run in production. Done when every candidate at or above 1.0 has a succeeded replace.

## Pointers

- GPU tests run only on the PC: media-encoder `scripts/pc-test.sh ./internal/ffmpeg`. The PC's `claude` account cannot reach the `youtube` share. Cut sample clips in the savetube scheduler pod with `ffmpeg -c copy` and copy them over.
- Measurements and their scripts: llm-wiki `projects/savetube-encode-fps.md`.
