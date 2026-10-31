# Re-encoding

An external encoder on the owner's Windows PC (RTX 5080) re-encodes stored H.264 videos to AV1 to save space. The encoder reads the source over Samba, encodes it, and drops the result in a folder inside `tank/youtube`. savetube checks the result and swaps it in, so savetube stays the only writer of the archive tree.

The encoder is its own private repo, `KOCEAN33/media-encoder`, at `/Users/kocean/codes/kocean33/media-encoder`. It serves other sources too. This file is the contract between the two. savetube owns it: the encoder side reads it, and a breaking change to a folder, file name or field needs the owner's approval first. Adding an optional field needs none.

## Which videos

- A `complete` video whose stored video stream is H.264, outside the max quality tier once tiers exist (candidates.md, Quality tiers), and whose last format probe offers no AV1 at the stored height or above. A video YouTube still serves as AV1 is downloaded again instead.
- Settings turn re-encoding on or off and pick the profile. The default profile is `vtuber-av1-cq46`.
- A video with a queued or running replace job, an open live recording, or an original already kept aside is skipped.

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

- savetube queues a bounded number of jobs, so the queue never holds the whole backlog.
- A user can cancel a queued job, which deletes its queue file. A claimed job is the encoder's until it moves on.

## Checking and swapping a result

- The scheduler looks in `done/` every minute. For each `.json` it checks the SHA-256 and probes the `.mkv`: the profile's codec, the source's duration within 1 s, and a video and an audio stream. A failed check moves both files to `failed/` with the reason.
- The swap reuses the replace job's swap (live.md, Format watch): the stored file is kept in `.originals/<video id>/` until `original_until`, 7 days later, then deleted. The result moves into the video directory by rename. `video.info`, `format_id` and `size_bytes` take the new streams.
- The video records that it was re-encoded, with the profile, so a later format probe or queueing never treats it as a fresh H.264 download.
