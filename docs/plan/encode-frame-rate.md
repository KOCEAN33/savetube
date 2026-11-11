# Plan: encode frame rate

Game and 3D live streams keep their 60 fps. Still streams go to 30 fps. A title names a still stream, and measured motion decides the rest. The encoder also decodes savetube jobs on NVDEC. Both repos change: savetube owns the contract in `encode.md`, and media-encoder (`/Users/kocean/codes/kocean33/media-encoder`) implements the measurement and the decode. Measurements behind every constant here: llm-wiki `projects/savetube-encode-fps.md`.

## Frame rate choice

savetube resolves each video in this order when it queues a job:

1. A title with a still word and no still exclude word takes `still_fps_cap`. No measurement runs.
2. Every other title takes `fps_cap` and `motion_threshold`. The encoder measures motion only when the source runs above `fps_cap`. A motion score at or above `motion_threshold` keeps the source's rate, and a lower one applies `fps_cap`.
3. A null `motion_threshold` applies `fps_cap` with no measurement. A null `fps_cap` keeps the source's rate with no measurement.

The defaults become `fps_cap` 30 and `motion_threshold` 1.0. `still_fps_cap` stays 30, and the still and exclude word lists stay as they are. The `3D` exclude word also matches `3dio` and `#3DOGS1YEAR`, but those titles then go to step 2, where their motion decides.

## Motion score

- Three 60 s windows start at 25%, 50% and 75% of the source's duration. A window past the end is cut short by the file's end.
- Per window: `ffmpeg -nostdin -v fatal -ss <start> -t 60 -i <src> -map 0:V:0 -vf "scale=320:180,format=gray,tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-" -f null -`, then the mean of every `lavfi.signalstats.YAVG` value. This equals the mean absolute luma difference between consecutive frames, which numpy gave as 0.73 and 6.34 on two test windows, the same values.
- The score is the median of the three window means, rounded to 3 decimals. Decode for the score is software decode, as in the measurements.
- `scripts/motion-mad.sh FILE...` computes the same score for any file. It prints the median, the three window means, and the path.

## Contract

- The job file's `options` gains the optional `motion_threshold`: a number above 0, or absent or null for no measurement.
- With `motion_threshold`, the derived profile name gains `-motion<threshold>` after the fps part, the threshold formatted like the fps cap: `av1-hq-cq46-30fps-motion1`.
- A job that measured adds `motion` (the score) and `fps` (the output rate, as ffprobe reports it, 3 decimals) to `done/<id>.json`. A job that did not measure adds neither.
- savetube's `_check` with `motion_threshold` set: `motion` must be present when the source runs above `fps_cap`. The wanted rate is the source's when `motion >= motion_threshold`, else `min(source, fps_cap)`, within `FPS_SLACK`. A missing or contradictory `motion` rejects the result.
- An encoder that predates `motion_threshold` ignores it and caps the rate, so its result fails the check whenever its motion would have kept the source rate. Release media-encoder first.

## NVDEC decode

- A savetube job decodes on NVDEC when the source's codec is `h264`, `hevc`, `vp9` or `av1` and its `pix_fmt` is `yuv420p`, `yuvj420p` or `yuv420p10le`. Every other source, and every edge job, keeps software decode. The edge profile's deinterlace and rotation filters run on the CPU.
- The NVDEC encode is `-hwaccel cuda -hwaccel_output_format cuda` before `-i`, and `-vf fps=<cap>,scale_cuda=format=p010le` in place of `fps=<cap>,format=p010le`. Without a cap the filter is `scale_cuda=format=p010le` alone.
- The probe gains `pix_fmt` from ffprobe's stream entry.
- Measured on the RTX 5080 with two encodes running: VP9 1080p60 software decode 664 fps on about 3.6 cores, NVDEC copied to system memory 381 fps, NVDEC with frames left on the GPU about 1,320 fps. Software and NVDEC frames matched by `-f framemd5` after `format=yuv420p`.
- `scripts/nvdec-check.bat` in media-encoder reruns the decode comparison on the PC.

## savetube settings

- `encode_settings` gains `motion_threshold` (float, nullable). The next migration adds it with default 1.0 and sets `fps_cap` to 30 on the existing row when it is null, since the motion decides what the null cap used to keep.
- `PUT /api/encode` takes `motion_threshold`: a number above 0, or null. The Encode page edits it beside the fps caps, empty meaning no measurement.
- `job_options` adds `motion_threshold` to the options of a step 2 title, and omits it for a still title.

## Re-downloading frame-lost videos

- The candidates are succeeded `encode_job` rows under `vtuber-av1-hq-cq46` or `vtuber-av1-cq46` whose video's `info.formats` lists a video format above 30 fps at `video_height`, and whose title has no still word. There were 210 on 2026-11-11.
- Score each candidate's stored file with `scripts/motion-mad.sh`, from a pod that mounts the archive. A 30 fps file scores up to twice its 60 fps value on moving content and about the same on still content, so the 1.0 threshold errs toward a re-download.
- Queue a replace for each candidate at or above 1.0. The replace supersedes the video's encode job, so the new download queues an encode under the settings above after its 7-day original keep (encode.md, Which videos).

## Motion measurements

Measured on 2026-11-11 with the Motion score method on 41 archive streams, 1080p60 sources at 60 fps. Kinds come from titles. `2_X5cOmOUYs` is labeled 3D for its `#3DOGS1YEAR` tag but is no 3D live.

| Kind | Video | Median | 25% / 50% / 75% |
| --- | --- | --- | --- |
| talk | `W_59lWecc8g` | 0.03 | 0.03 / 0.02 / 0.03 |
| talk | `_VJvCWsrWho` | 0.03 | 0.02 / 0.03 / 0.03 |
| talk | `m8rcIxT76kw` | 0.16 | 0.14 / 0.16 / 0.22 |
| talk | `Iv7flN4-E7k` | 0.18 | 0.13 / 0.18 / 0.80 |
| talk | `NFAA1nQ_YTs` | 0.21 | 0.12 / 0.21 / 0.42 |
| talk | `Vm3VfmlhnlQ` | 0.44 | 0.39 / 0.44 / 0.85 |
| talk | `NHOcAZ9jh58` | 0.47 | 0.47 / 0.40 / 0.62 |
| talk | `rJJjGYbapLs` | 0.52 | 0.52 / 0.31 / 0.58 |
| talk | `jFRFuhVqgqE` | 0.55 | 0.56 / 0.55 / 0.49 |
| talk | `APTAHwy1N5g` | 0.60 | 0.51 / 0.60 / 0.89 |
| talk | `_1bvahGJHLQ` | 0.73 | 0.44 / 0.73 / 0.86 |
| talk | `UdxAW3iUnhw` | 0.74 | 0.66 / 0.74 / 0.77 |
| talk | `g0pUAphd6Bc` | 1.04 | 1.04 / 0.77 / 1.43 |
| talk | `T-Mt3cuZ6mU` | 3.44 | 3.44 / 1.70 / 4.18 |
| singing | `owPJV2NSLXU` | 0.13 | 0.12 / 0.15 / 0.13 |
| singing | `KX8p1RKqnwg` | 0.20 | 0.23 / 0.20 / 0.12 |
| singing | `686_nv8CZP0` | 0.21 | 0.44 / 0.21 / 0.20 |
| singing | `eEB_-C7j0-8` | 0.38 | 0.38 / 0.37 / 0.45 |
| singing | `Vxb8XD498Jo` | 0.69 | 0.71 / 0.69 / 0.69 |
| singing | `NJNE7K_KzkM` | 0.80 | 0.77 / 0.80 / 1.02 |
| singing | `YPgiYgjrCdk` | 1.05 | 1.05 / 0.87 / 1.73 |
| game | `x-qzKWifMNk` | 0.33 | 0.49 / 0.33 / 0.12 |
| game | `dDPkZ5hN7L8` | 0.94 | 0.94 / 0.33 / 0.98 |
| game | `VgK98GE9vw4` | 1.72 | 1.72 / 1.47 / 1.96 |
| game | `nMTQyZ70TCQ` | 2.79 | 1.91 / 3.10 / 2.79 |
| game | `O-rmDXUM7Kw` | 3.00 | 2.50 / 3.00 / 3.22 |
| game | `WNBiQy0H-1k` | 3.24 | 5.97 / 3.24 / 2.56 |
| game | `dUOGM1xuQ7k` | 3.24 | 3.24 / 1.70 / 3.93 |
| game | `6c_E_TuQDzE` | 3.90 | 3.90 / 6.34 / 0.63 |
| game | `zstSY05ToqE` | 4.42 | 5.07 / 4.42 / 2.77 |
| game | `NLnR6TEu5qk` | 5.83 | 5.98 / 5.83 / 5.68 |
| game | `mo-9b5Ypbk0` | 6.81 | 6.81 / 7.06 / 5.67 |
| game | `atF1zYamhJc` | 8.41 | 0.35 / 8.41 / 10.46 |
| game | `eSerOybMuTI` | 9.54 | 13.49 / 9.54 / 0.48 |
| game | `haDQdZrnZ3Y` | 10.78 | 6.85 / 10.78 / 11.16 |
| 3D live | `2_X5cOmOUYs` | 0.22 | 0.38 / 0.16 / 0.22 |
| 3D live | `V64NpRs9rsQ` | 1.13 | 1.02 / 1.13 / 1.34 |
| 3D live | `bJuzteJ-fsE` | 3.28 | 3.28 / 2.47 / 3.51 |
| 3D live | `CSCwoNQHCRc` | 3.53 | 4.64 / 1.38 / 3.53 |
| 3D live | `ZpAJBxTsuTE` | 4.82 | 4.82 / 5.81 / 0.37 |
| 3D live | `vzQcOVGZEig` | 7.42 | 7.42 / 9.55 / 5.75 |
| 3D live | `DHWhsECviCc` | 7.83 | 12.59 / 3.47 / 7.83 |
| 3D live | `-Zn37N2LeRM` | 9.33 | 9.16 / 9.33 / 9.58 |

The same streams' stored 30 fps encodes, against the 60 fps median above:

| Video | 30 fps median | 25% / 50% / 75% |
| --- | --- | --- |
| `m8rcIxT76kw` | 0.15 | 0.14 / 0.15 / 0.22 |
| `Iv7flN4-E7k` | 0.18 | 0.13 / 0.18 / 0.82 |
| `NFAA1nQ_YTs` | 0.21 | 0.12 / 0.21 / 0.41 |
| `686_nv8CZP0` | 0.38 | 0.79 / 0.38 / 0.34 |
| `x-qzKWifMNk` | 0.49 | 0.74 / 0.49 / 0.18 |
| `eEB_-C7j0-8` | 0.63 | 0.63 / 0.62 / 0.73 |
| `V64NpRs9rsQ` | 1.20 | 1.03 / 1.20 / 1.35 |
| `CSCwoNQHCRc` | 6.43 | 7.76 / 2.52 / 6.43 |
| `zstSY05ToqE` | 6.50 | 7.60 / 6.50 / 4.41 |
| `vzQcOVGZEig` | 7.31 | 7.31 / 9.52 / 5.75 |
| `6c_E_TuQDzE` | 7.34 | 7.34 / 12.17 / 1.14 |

## Steps

1. media-encoder: NVDEC decode as in NVDEC decode. Done when `scripts/pc-test.sh ./internal/ffmpeg` passes with a new test that a VP9 1080p60 sample encodes through NVDEC at the source's rate, and the existing rotation and deinterlace tests still pass on software decode.
2. savetube: `encode.md` takes the Contract section, before code in either repo.
3. media-encoder: `job.Options.MotionThreshold`, the motion score, and the `motion` and `fps` done fields. Done when the score test on a generated still clip and a generated moving clip lands on either side of 1.0, and the savetube source test shows the two fields in `done/<id>.json`.
4. savetube: the migration, the API field, the Encode page field, `job_options`, the derived name, and `_check`. Done when `scripts/test-db.sh make check` passes with tests for a still title, a measured job at and below the threshold, and a missing `motion`.
5. Release media-encoder, then ask the owner for a savetube release.
6. Re-download as in Re-downloading frame-lost videos, after both releases run in production.
