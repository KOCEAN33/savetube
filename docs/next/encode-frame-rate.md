# Encode frame rate

## Goal

Game and 3D live streams keep 60 fps and still streams go to 30 fps, decided by measured motion, with savetube jobs decoded on NVDEC. Then the frame-lost videos that need 60 fps are downloaded again. Design, constants and done checks: `docs/plan/encode-frame-rate.md`.

## Start

Step 1 of `docs/plan/encode-frame-rate.md`, Steps: NVDEC decode in media-encoder (`/Users/kocean/codes/kocean33/media-encoder`, `internal/ffmpeg/ffmpeg.go`). The steps run in the plan's order, because the contract and both releases depend on it.

- Release media-encoder before asking the owner for a savetube release. A savetube job with `motion_threshold` fails its check on an encoder that ignores the field.
- The owner chose to download again only the frame-lost videos that need 60 fps, picked by motion score, not all 210 (plan, Re-downloading frame-lost videos).

## Next steps

1. Plan steps 1-5, each done when its check in the plan passes.
2. Plan step 6 after both releases run in production. Done when every candidate at or above 1.0 has a succeeded replace.

## Pointers

- GPU tests run only on the PC: media-encoder `scripts/pc-test.sh ./internal/ffmpeg`. The PC's `claude` account cannot reach the `youtube` share. Cut sample clips in the savetube scheduler pod with `ffmpeg -c copy` and copy them over.
- Measurements and their scripts: llm-wiki `projects/savetube-encode-fps.md`.
- The worktree `.claude/worktrees/agent-aa7454bd07ce107ca` (branch `worktree-agent-aa7454bd07ce107ca`, merged into develop as `0325bf5`) is locked by its finished agent. Remove it with `git worktree remove` once the lock is gone.
