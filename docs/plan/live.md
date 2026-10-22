# Live

## Live detection

- A watched channel has "watch lives" on and at least one enabled tab source.
- Every minute, per watched channel, the scheduler reads three places: the RSS feed, the first page of the Live tab, and the `/live` page.
- The RSS feed holds only the 15 newest uploads. On a busy channel a waiting room drops out of it days before the stream, so the Live tab's `is_upcoming` and `is_live` entries are candidates too.
- Each candidate id is probed once per scheduler process with yt-dlp and `ignore_no_formats_error`. Without that option an upcoming stream raises "This live event will begin in ..." instead of returning `release_timestamp`. The Live tab listing and the probes use the general cookie profile. The RSS feed takes no cookies. A probe that fails with a members-only error is probed once more with the channel's first member profile by name, if one is present. A probe that still fails, such as a private video or a members-only video no profile may watch, is skipped.
- A detection through the member profile, or whose probe reports `availability` `subscriber_only`, stores its video as type `members`. Its recording then takes a member profile (access.md, Cookies and account).
- A members-only detection of a channel with `members_all` on passes the title words of any watching source (archive.md, Title words).
- The `/live` page probe takes the general profile only.
- The `/live` page finds a stream with no waiting room. "The channel is not currently live" is the normal answer and is not an error.
- No Data API key is used. The probe reads the scheduled start that `videos.list` would.
- A detection is a queued `record` job. `scheduled_at` comes from `release_timestamp` for an upcoming stream and is null for a stream found live. The record job keeps a video to one detection across scheduler restarts.
- The first check of a channel probes up to 15 feed ids and takes about a minute. Later checks take seconds.
- A queued record job does not stop the archive poll from queuing the VOD. Until a recorder exists, the VOD is the only copy of a detected live.
- WebSub push needs a public HTTPS callback, which the tailnet-only cluster lacks. Not used.

## Recording and the VOD

- An upcoming stream gets a recorder Job a few minutes before `scheduled_at`, running with `--wait-for-video` and `--live-from-start`. Not built.
- A live recording can have gaps or buffering, so when both a recording and the VOD exist, the two are compared. How to compare and which to keep is open.
- Some streams get no VOD after they end. For those, the recording is the only copy and stays.
