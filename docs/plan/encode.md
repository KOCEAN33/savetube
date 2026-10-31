# Re-encoding

An external encoder on the owner's Windows PC (RTX 5080) re-encodes stored H.264 videos to AV1 to save space. The encoder reads the source over Samba, encodes it, and drops the result in a folder inside `tank/youtube`. savetube checks the result and swaps it in, so savetube stays the only writer of the archive tree.

The encoder is its own private repo, `KOCEAN33/media-encoder`, at `/Users/kocean/codes/kocean33/media-encoder`. It serves other sources too. This file is the contract between the two. savetube owns it: the encoder side reads it, and a breaking change to a folder, file name or field needs the owner's approval first. Adding an optional field needs none.

## Which videos

- A `complete` video whose stored video stream is H.264, outside the max quality tier once tiers exist (candidates.md, Quality tiers), and whose last format probe offers no AV1 at the stored height or above. A video YouTube still serves as AV1 is downloaded again instead.
- Settings turn re-encoding on or off and pick the profile. The default profile is `vtuber-av1-cq46`.
- A video with a queued or running replace job, an open live recording, an open format watch, or an original already kept aside is skipped. So is a video with an encode job queued, claimed, failed or succeeded.
- The last format probe is the format watch's stored list, else the format list of the download's info dict. A video with neither counts as offered AV1.
- The largest stored videos queue first.

## Profiles

A profile names a fixed encode. The encoder repo owns each profile's ffmpeg arguments. savetube sends only the name and checks the result against the profile's output, so a profile never changes meaning: new settings get a new name.

| Profile | Output |
| --- | --- |
| `vtuber-av1-cq46` | AV1 10-bit (`av1_nvenc`, `-tune uhq`, `-cq 46`, hierarchical B-frames), 30 fps when the source runs above 30, the source's resolution, audio streams copied, Matroska |

The profile choice rests on a measured 10-minute clip of `hSLDqEi-yCE` (1080p60 H.264, 3.3 Mbps): 24% of the source's video bytes at VMAF 91.6.

## Shares

The encoder reaches `tank/youtube` as one Samba share, `\\192.168.69.31\youtube`, with the login `encoder`. The share is read-only except `.encode/`, where files land owned by uid 1000 like the rest of the archive. Sources are read from the share, and job files move inside `.encode/`. proxmox-talos-lab ADR encoder-share sets this up.

savetube sees the same folder as `/archive/.encode`. Every walk of the archive tree skips `.encode`, as it skips `.incoming` and `.originals`. Samba's trash for the share is `.encode/.recycle`, which holds files the encoder deleted for 30 days.

## Job files

Each job is a file that moves between folders under `.encode/`. A move is a rename on one share, so a job is in exactly one folder.

| Folder | Written by | Holds |
| --- | --- | --- |
| `queue/<job id>.json` | savetube | `{job_id, video_id, source, profile, created_at}`. `source` is the path of the stored file relative to the `youtube` share root, which is the archive root, with `/` separators |
| `claimed/<job id>.json` | encoder, by renaming the queue file | the job the encoder works on. A paused or restarted encoder resumes its claimed jobs first |
| `work/<job id>.mkv` | encoder | the finished output while it is copied in. The encoder encodes on its local disk and copies only the complete file here. savetube never reads `work/` |
| `done/<job id>.mkv` and `done/<job id>.json` | encoder | the result, moved from `work/` when complete, then the job file with `{profile, encoder, ffmpeg_version, sha256, size, duration, finished_at}` added. The `.json` is written last, so its presence means the `.mkv` is complete |
| `failed/<job id>.json` | encoder | the job file with `{reason, log_tail, failed_at}` added. The video is not queued again until a user requeues it |

- savetube keeps up to 3 jobs in `queue/`, so the queue never holds the whole backlog.
- savetube writes a job file under a temporary name in the same folder, then renames it. Only a name ending in `.json` is a job file.
- A queued or claimed job whose file no folder holds for 10 minutes is lost, and its video may queue again.
- A user can requeue a failed job. savetube then deletes its files in `failed/`, and the video may queue again.
- A user can cancel a queued job, which deletes its queue file. A claimed job is the encoder's until it moves on.

## Checking and swapping a result

- The scheduler looks in `done/` every minute. For each `.json` it checks the profile, the size and the SHA-256 against the job file, then probes the `.mkv` and the stored file. The profile's codec, bit depth and frame rate, the source's resolution, the source's audio stream count, the source's duration within 1 s, and a clean packet pass must hold.
- The result must be smaller than the source, and the stored file must still be the one the job named, with the same size.
- A failed check moves both files to `failed/` with the reason, the `.mkv` first.
- While a replace job or a recording is queued or running for the video, the result waits in `done/`.
- The swap reuses the replace job's swap (live.md, Format watch): the stored file is kept in `.originals/<video id>/` until `original_until`, 7 days later, then deleted. The result moves into the video directory by rename. `video.info`, `format_id` and `size_bytes` take the new streams.
- The video records that it was re-encoded, with the profile, so a later format probe or queueing never treats it as a fresh H.264 download. The stored video stream in `video.info` takes the profile name as `format_id`, `re-encoded` as `format_note` and `av01` as `vcodec`. The audio streams keep their YouTube formats.
- Settings, Re-encoding holds the on/off switch, the profile, and the job list with Cancel on a queued job and Requeue on a failed one. Turning re-encoding off queues no more jobs and leaves the queued ones.
