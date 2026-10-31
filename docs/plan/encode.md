# Re-encoding

An external encoder on the owner's Windows PC (RTX 5080) re-encodes stored H.264 videos to AV1 to save space. savetube stays the only writer of `tank/youtube`: the encoder pulls a job over HTTP, downloads the source, encodes it, and uploads the result. savetube checks and swaps the file.

The encoder is its own private repo, `KOCEAN33/media-encoder`, at `/Users/kocean/codes/kocean33/media-encoder`. It serves other sources too. This file is the contract between the two. savetube owns it: the encoder side reads it, and a breaking change to a route or field needs the owner's approval first. Adding an optional field needs none.

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

## Routes

The encoder authenticates with `Authorization: Bearer <token>`. The token is a savetube setting, not a Zitadel login. These routes accept only the token, and the token opens no other route.

| Route | Does |
| --- | --- |
| `POST /api/encode/claim` | body `{worker, profiles}`, the worker's name and the profiles it can run. Answers 200 `{job_id, video_id, profile, source: {url, size, sha256}, lease_seconds}` for the next video, or 204 when none waits. The claim holds the video for `lease_seconds` |
| `GET <source.url>` | the stored file, with `Range` support so a broken download resumes. Opened with the same token |
| `POST /api/encode/{job_id}/heartbeat` | body `{state, progress}`, state `downloading`, `encoding`, `uploading` or `paused`, progress 0-1. Renews the lease. 409 when the job is no longer this worker's |
| `POST /api/encode/{job_id}/release` | gives the job back without blame, such as a pause that lasts too long. The video waits for the next claim |
| `POST /api/encode/{job_id}/fail` | body `{reason, log_tail}`. Ends the job failed. The video is not offered again until a user requeues it |
| `PUT /api/encode/{job_id}/result` | body: the encoded file. Headers `X-Encode-Profile`, `X-Encoder` (ffmpeg version and encoder name) and `X-Content-SHA256`. Answers 200 `{status: "swapped", size}` after the swap, or 422 `{reason}` when the check fails |

- A lease that runs out without a heartbeat returns the job to the queue. The encoder sends a heartbeat at least every 60 s, also while paused.
- A result upload is about a quarter of the source, near 600 MB for a 100-minute 1080p stream. The ingress and the web pod must take a body that size.

## Checking and swapping a result

- The upload lands in staging first. savetube checks the SHA-256, then probes the file: the profile's codec, the source's duration within 1 s, and a video and an audio stream.
- The swap reuses the replace job's swap (live.md, Format watch): the stored file is kept in `.originals/<video id>/` until `original_until`, 7 days later, then deleted. `video.info`, `format_id` and `size_bytes` take the new streams.
- The video records that it was re-encoded, with the profile, so a later format probe or a second claim never treats it as a fresh H.264 download.
