# Re-encoding

An external encoder on the owner's Windows PC (RTX 5080) re-encodes stored H.264 and VP9 videos to AV1 to save space. The encoder reads the source over Samba, encodes it, and drops the result in a folder inside `tank/youtube`. savetube checks the result and swaps it in, so savetube stays the only writer of the archive tree.

The encoder is its own private repo, `KOCEAN33/media-encoder`, at `/Users/kocean/codes/kocean33/media-encoder`. It serves other sources too. This file is the contract between the two. savetube owns it: the encoder side reads it, and a breaking change to a folder, file name or field needs the owner's approval first. Adding an optional field needs none.

## Which videos

- A `complete` video whose stored video stream is H.264 or VP9, outside the max quality tier once tiers exist (candidates.md, Quality tiers), and whose last format probe offers no AV1 at the stored height or above. A video YouTube still serves as AV1 is downloaded again instead.
- Settings turn re-encoding on or off and hold the encode options. A video's title can give its job the still frame rate cap (Profiles).
- A video with a queued or running replace job, an open live recording, an open format watch, or an original already kept aside is skipped. So is a video with an encode job queued, claimed, failed or succeeded.
- The last format probe is the format watch's stored list, else the format list of the download's info dict. A video with neither counts as offered AV1.
- A stored file under 10 MiB is skipped: it saves little, and a low-bitrate one can come out larger than its source.
- The largest stored videos queue first.

## Profiles

Each job file carries the encode options, `{tune, cq, fps_cap, motion_threshold}`. The encoder composes its ffmpeg arguments from them, so savetube's settings change an encode without a change on the encoder side. Every encode gives AV1 10-bit (`av1_nvenc`, hierarchical B-frames) at the source's resolution, with the audio streams copied, in Matroska. The options set the rest:

| Option | Meaning |
| --- | --- |
| `tune` | `hq` or `uhq`, passed as `-tune` |
| `cq` | an integer from 1 to 63, passed as `-cq` |
| `fps_cap` | a number: the output runs at the source's frame rate or this cap, whichever is lower. Null keeps the source's frame rate |
| `motion_threshold` | optional, a number above 0. With it, the encoder measures the motion score of a source above `fps_cap`: a score at or above the threshold keeps the source's frame rate, and a lower one applies `fps_cap`. Absent or null measures nothing and applies `fps_cap` |

The motion score is the median of three window means, rounded to 3 decimals. The three 60 s windows start at 25%, 50% and 75% of the source's duration, and a window past the end is cut short by the file's end. A window's mean is the mean of every `lavfi.signalstats.YAVG` value from `ffmpeg -nostdin -v fatal -ss <start> -t 60 -i <src> -map 0:V:0 -vf "scale=320:180,format=gray,tblend=all_mode=difference,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-" -f null -`, with software decode: the mean absolute luma difference between consecutive frames. `scripts/motion-mad.sh FILE...` computes the same score.

The Encode page edits the settings these options come from:

- `tune`, `cq` and `fps_cap`, for every video. The defaults are `hq`, 46 and 30.
- `motion_threshold`, default 1.0. A title that is not still carries it in its options when `fps_cap` is set, so its motion decides between the source's rate and `fps_cap`. Null applies `fps_cap` without measuring. On 41 measured 1080p60 streams, talk and singing streams mostly scored below 1.0, and game and 3D live streams mostly above (`encode-frame-rate.md`, Motion measurements).
- `still_fps_cap`, the cap a still stream's job takes instead of `fps_cap`, default 30. Null keeps the source's frame rate. A still title measures no motion: a mostly still stream gains little from 60 fps.
- `still_words`, default `歌枠`, `うたわく`, `ASMR`, `雑談`, `karaoke`, `singing stream`, `free talk`, `freetalk`. A title with one of them is a still stream. With no still words, no title is.
- `still_exclude_words`, default `3D`. A title with one of them never counts as still: a 3D live often names `歌枠` too.
- The match is case-insensitive substring, as for a source's title words (`savetube.words.matches`).

The job file's `profile` is a name the options derive: `av1-{tune}-cq{cq}-{fps}`, where `{fps}` is `srcfps` for a null cap, else the cap without a trailing `.0` and `fps`, such as `av1-hq-cq46-30fps` or `av1-uhq-cq40-srcfps`. With `motion_threshold`, the name gains `-motion{threshold}` after the fps part, the threshold formatted like the cap: `av1-hq-cq46-30fps-motion1`. The re-encoded stream's `format_id`, the job list and the done file's profile check use it.

A job file queued before the options has no `options`, only a named profile. The encoder falls back to the named profile's arguments, and savetube checks the result against that profile's output. Nothing queues under these names any more:

| Profile | Output |
| --- | --- |
| `vtuber-av1-hq-cq46` | `tune` `hq`, `cq` 46, `fps_cap` 30 |
| `vtuber-av1-uhq-cq46` | `tune` `uhq`, `cq` 46, `fps_cap` 30 |
| `vtuber-av1-hq-cq46-srcfps` | `tune` `hq`, `cq` 46, the source's frame rate |
| `vtuber-av1-uhq-cq46-srcfps` | `tune` `uhq`, `cq` 46, the source's frame rate |
| `vtuber-av1-cq46` | the retired name of `vtuber-av1-uhq-cq46` |

The default `tune` and `cq` rest on a measured 10-minute clip of `hSLDqEi-yCE` (1080p60 H.264, 3.3 Mbps), with VMAF scored against the source resampled to 30 fps. `hq` at `cq` 46 and a 30 fps cap gives 18% of the source's video bytes at VMAF 86.8 in 47 s. `uhq` gives 24% at VMAF 89.3 in 100 s. The source streams are low-bitrate, so the default takes the smaller, faster `hq`.

## Shares

The encoder reaches `tank/youtube` as one Samba share, `\\192.168.69.31\youtube`, with the login `encoder`. The share is read-only except `.encode/`, where files land owned by uid 1000 like the rest of the archive. Sources are read from the share, and job files move inside `.encode/`. proxmox-talos-lab ADR encoder-share sets this up.

savetube sees the same folder as `/archive/.encode`. Every walk of the archive tree skips `.encode`, as it skips `.incoming` and `.originals`. Samba's trash for the share is `.encode/.recycle`, which holds files the encoder deleted for 30 days.

## Job files

Each job is a file that moves between folders under `.encode/`. A move is a rename on one share, so a job is in exactly one folder.

| Folder | Written by | Holds |
| --- | --- | --- |
| `queue/<job id>.json` | savetube | `{job_id, video_id, source, profile, options, created_at}`. `source` is the path of the stored file relative to the `youtube` share root, which is the archive root, with `/` separators. `options` is `{tune, cq, fps_cap, motion_threshold}` resolved for this video's title, and `profile` the name it derives (Profiles). A job file without `options` carries a named profile |
| `claimed/<job id>.json` | encoder, by renaming the queue file | the job the encoder works on. A paused or restarted encoder resumes its claimed jobs first |
| `work/<job id>.mkv` | encoder | the finished output while it is copied in. The encoder encodes on its local disk and copies only the complete file here. savetube never reads `work/` |
| `done/<job id>.mkv` and `done/<job id>.json` | encoder | the result, moved from `work/` when complete, then the job file with `{profile, encoder, ffmpeg_version, sha256, size, duration, finished_at}` added. A job that measured motion also adds `motion`, the score, and `fps`, the output frame rate as ffprobe reports it, to 3 decimals. The `.json` is written last, so its presence means the `.mkv` is complete |
| `failed/<job id>.json` | encoder | the job file with `{reason, log_tail, failed_at}` added. The video is not queued again until a user requeues it |

- savetube keeps up to 3 jobs in `queue/`, so the queue never holds the whole backlog.
- savetube writes a job file under a temporary name in the same folder, then renames it. Only a name ending in `.json` is a job file.
- A queued or claimed job whose file no folder holds for 10 minutes is lost, and its video may queue again.
- A user can requeue a failed job. savetube then deletes its files in `failed/`, and the video may queue again.
- A user can cancel a queued job, which deletes its queue file. A claimed job is the encoder's until it moves on.

## Checking and swapping a result

- The scheduler looks in `done/` every minute. For each `.json` it checks the profile, the size and the SHA-256 against the job file, then probes the `.mkv` and the stored file. AV1, 10-bit, the frame rate the job's options set (or its named profile's, for a job without options), the source's resolution, the source's audio stream count, the source's duration within 1 s, and a clean packet pass must hold.
- With `motion_threshold`, a source above `fps_cap` needs `motion` in the done file. The wanted frame rate is the source's when `motion >= motion_threshold`, else `min(source, fps_cap)`, within `FPS_SLACK`. A missing or contradictory `motion` fails the check. An encoder that predates `motion_threshold` ignores it and applies the cap, so its result fails whenever the motion would have kept the source's rate.
- The result must be smaller than the source, and the stored file must still be the one the job named, with the same size.
- A failed check moves both files to `failed/` with the reason, the `.mkv` first.
- While a replace job or a recording is queued or running for the video, the result waits in `done/`.
- The swap reuses the replace job's swap (live.md, Format watch): the stored file is kept in `.originals/<video id>/` until `original_until`, 7 days later, then deleted. The result moves into the video directory by rename. `video.info`, `format_id` and `size_bytes` take the new streams.
- The video records that it was re-encoded, with the profile, so a later format probe or queueing never treats it as a fresh YouTube download. The stored video stream in `video.info` takes the profile name as `format_id`, `re-encoded` as `format_note` and `av01` as `vcodec`. The audio streams keep their YouTube formats.
- The Encode page (frontend.md) holds the on/off switch, the encode options, and the job list with Cancel on a queued job and Requeue and Dismiss on a failed one. A dismissed failed job still keeps its video from queueing. Turning re-encoding off queues no more jobs and leaves the queued ones.
