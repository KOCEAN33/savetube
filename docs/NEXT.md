# Next

## Goal

Confirm in production the released format fallback, recorder, and HTTP 403 changes.

## Next steps

1. No production job has reached `job.done` after a format fallback (unverified). When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count. Job 4429 `oT5iycnnqQs` fell back from 399 to 303 on HTTP 500, then failed on the members-only HTTP 403 that 0.32.0 fixes, so it does not count.
2. Production has not hit the recorder's give-up path (unverified). When a waiting room stays upcoming 24 hours past its start, check that the recorder logs `record.not_started`, its row shows `missed`, and its Job ends `succeeded` with no retry pod.
3. The `main` queue behind the 403 client fallback drains one job at a time, about 3-13 minutes each. Jobs 4710-4713 each logged `download.client_fallback clients=mweb`, then `job.done`, and `dispatch.profile_unblocked` cleared the `main` pause. Check in Loki that the remaining `main` jobs from 4714 on reach `job.done`, and that no `dispatch.profile_blocked` follows a `download.client_fallback`. The scheduler's `queue.summary` shows `profiles_blocked=none blocked=none`, so the header badge has no data to show. The user checks that it is gone in the production UI.

## Encode frame rate

Goal: game and 3D live streams keep their 60 fps, still streams go to 30 fps, chosen by measured motion. Research and measurements: llm-wiki `projects/savetube-encode-fps.md`. The steps run in this order.

1. media-encoder decodes on NVDEC with frames left on the GPU: `-hwaccel cuda -hwaccel_output_format cuda`, `scale_cuda=format=p010le` in place of `format=p010le`, and a CUDA deinterlacer for the file source. A source NVDEC cannot decode falls back to software decode. Done when `scripts/pc-test.sh ./internal/ffmpeg` passes and a 1080p60 VP9 job runs above the 4.8x it ran at with CPU decode.
2. encode.md gains an optional `options.motion_threshold`. With it set, the encoder measures the median consecutive-frame luma MAD of at least three 60 s windows at the source rate, keeps the source rate at or above the threshold, and applies `fps_cap` below it. The done file reports the measured value and the rate used, and `_check` takes that rate. Edit encode.md before either repo's code.
3. media-encoder implements step 2. savetube adds the threshold, default 1.0, and a source-rate title word list for games and 3D live to the Encode page. The `3D` exclude word also matches `3dio`, a binaural mic name, so the default needs a narrower word.
4. Release media-encoder first, then ask the owner for a savetube release: a job with `motion_threshold` fails on an encoder that does not know it.
5. Re-download the frame-lost videos that need 60 fps. The 210 videos are succeeded jobs under `vtuber-av1-hq-cq46` or `vtuber-av1-cq46` whose `info.formats` offers above 30 fps at the stored height and whose title has no still word. Measure their stored 30 fps files, then queue a replace for those at or above the threshold or with a game or 3D title. A 30 fps file scores up to twice its 60 fps value on moving content, so the 60 fps threshold errs toward a re-download.
