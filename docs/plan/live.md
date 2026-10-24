# Live

## Live detection

- A watched channel has "watch lives" on and at least one enabled tab source.
- Every minute, per watched channel, the scheduler reads three places: the RSS feed, the first page of the Live tab, and the `/live` page.
- The RSS feed holds only the 15 newest uploads. On a busy channel a waiting room drops out of it days before the stream, so the Live tab's `is_upcoming` and `is_live` entries are candidates too.
- Each candidate id is probed once per scheduler process with yt-dlp and `ignore_no_formats_error`. Without that option an upcoming stream raises "This live event will begin in ..." instead of returning `release_timestamp`. The Live tab listing and the probes use the general cookie profile. The RSS feed takes no cookies. A probe that fails with a members-only error is probed once more with the channel's first member profile by name, if one is present. A probe that still fails, such as a private video or a members-only video no profile may watch, is skipped.
- A detection through the member profile, or whose probe reports `availability` `subscriber_only`, stores its video as type `members`. Its recording then takes a member profile (access.md, Cookies and account).
- A members-only detection of a channel with `members_all` on passes the title words of any watching source (archive.md, Title words).
- Every detection of a channel with `keep_all` on passes the title words of any watching source.
- The `/live` page probe takes the general profile only.
- The `/live` page finds a stream with no waiting room. "The channel is not currently live" is the normal answer and is not an error.
- No Data API key is used. The probe reads the scheduled start that `videos.list` would.
- A detection is a queued `record` job. `scheduled_at` comes from `release_timestamp` for an upcoming stream and is null for a stream found live. The record job keeps a video to one detection across scheduler restarts.
- The first check of a channel probes up to 15 feed ids and takes about a minute. Later checks take seconds.
- A queued record job does not stop the archive poll from queuing the VOD. The poll skips a live until it ends, so the VOD is queued only when the recording has not stored the video.
- WebSub push needs a public HTTPS callback, which the tailnet-only cluster lacks. Not used.

## Recording and the VOD

- Every 15 s the scheduler starts a recorder Job for each queued record job due within 5 minutes: `scheduled_at` within 5 minutes, already past, or null for a stream found live. The recorder runs `savetube record --job-id <id>` with `wait_for_video` and `live_from_start` (`backend/savetube/recorders.py`).
- The scheduler lists Jobs by the `savetube.io/job-id` label, so a job with a Job, finished or not, never gets a second. A recorder Job that spent its `backoffLimit` while its row is still queued or running marks the row `missed` with `recorder_failed` and the video `failed`.
- The loop runs only inside a cluster, where `KUBERNETES_SERVICE_HOST` is set. The compose scheduler makes no recorder Jobs.
- A recording skips the `live_chat` track. yt-dlp writes subtitles before the media, and a running live's chat downloads until the stream ends. The VOD's archive keeps the chat replay.
- A recording writes its progress to `job.progress` every 5 s, as an archive download does, through the same pump on a thread instead of a child process. The video and audio streams download at once, so `done` and `speed` sum over the streams. A live has no total, so `pct` and `total` stay null. The recorder sends no format pin and no stream sizes: those serve the archive resume and its checks, and a VOD queued later must not take a live format.
- A finished recording moves straight into the tree under the video's `lives` or `members` directory and marks the video complete.
- Some streams get no VOD after they end. For those, the recording is the only copy and stays.

## Format watch

YouTube serves a live's VOD first in H.264 and AAC-or-Opus, and adds VP9 or AV1 at full resolution hours or days later, if ever. A live stream serves only AAC audio (`140`), while the VOD serves Opus (`251`) from the start. The user keeps lives mostly for the audio.

- Every live video gets a format watch for 7 days from the moment it turns `complete`: a finished recording, or a VOD download of a live (`was_live`). Members-only lives are included.
- Every 6 hours the scheduler probes each watched video's formats, with the general profile, or the member profile for type `members`. It stores the format list and the VOD duration.
- Only `https` formats count. HLS (`m3u8`) copies of the same stream are ignored. Audio formats with `-drc` in the id are ignored, because dynamic range compression flattens singing.
- Audio pick: Opus over AAC, then the higher `abr`.
- Video pick: among formats with height and fps at least the stored stream's, AV1 over VP9 over H.264, then the higher `tbr` within one codec. Bitrates of different codecs are not compared.
- A pick that ranks above the stored stream queues a replace job. Audio and video are judged apart: a replace downloads only the stream that changes and remuxes it with the stored other stream through `ffmpeg -c copy`.
- Before any replace, the VOD duration is compared with the stored file's `ffprobe` duration. A difference over 2 s holds the replace and the UI shows it with the difference in seconds. The 2 s is fixed, not the 1% of the download check.
- A replace passes the same integrity checks as a download, then swaps the file in the tree and writes a new audio copy.
- The first stored file, the recording or the first VOD download, moves aside and stays until the watch ends, then is deleted. A file from an intermediate replace is deleted at the next replace.
- The video page lists the stored file and every counted YouTube format: codec, resolution and fps, bitrate, size, duration, and the duration difference. The pick carries a `recommended` badge with a one-line reason.
- The user can pick any listed format there, a held video included after a confirm. A manual pick ends the automatic replaces for that video.
- A video with no VOD keeps its recording. The watch ends without a replace.
