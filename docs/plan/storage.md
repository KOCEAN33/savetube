# Storage

## Data model

| Table | Holds |
| --- | --- |
| `channel` | channel id, handle, display name, avatar URL, the directory name fixed at first add, include and exclude title words, date cutoff, format selector, watch lives |
| `cookie_profile` | the cookie profile names the scheduler last found in `SAVETUBE_COOKIES_DIR`, each with the time it was seen |
| `membership` | per channel and cookie profile: whether the profile's account is a member, when the check ran, the members-only video it read |
| `global_words` | one row: the global include and exclude title words |
| `source` | a channel tab, a channel's playlists, or a playlist URL, enabled flag, archiving flag, poll interval, include and exclude title words, other-channel flag, parent source, last poll time and error |
| `listing_entry` | every video a source's last listings showed: source, video id, channel, title. Title word suggestions read it |
| `video` | video id, channel, type, title, upload date, duration, live status, availability, format ids, file paths, `size_bytes`, `completed_at`, state, the pinned format of a partial download, the integrity checks it passed and when |
| `video_source` | which sources reference a video, so a playlist never copies a file |
| `job` | kind (archive, record), state, attempts, miss reason, log tail, the running download's progress, timestamps |
| `storage_sample` | hourly free and used bytes of the dataset |
| `video.info` | the full yt-dlp info dict as JSONB |

- `channel.avatar_url` is the smallest square thumbnail of a tab listing, else its `avatar_uncropped` original. The scan finds it for a new channel, and every tab poll refreshes it. A playlist listing carries the playlist's thumbnails, so a playlist poll leaves it alone.
- Migration `0011` adds `download_settings.paused`, false on upgrade. Its downgrade drops it.
- Migration `0010` adds `cookie_profile`, `membership` and `download_settings.cookie_profile`, and drops `channel.use_cookies`. Its downgrade adds `use_cookies` back as false on every channel.
- Migration `0008` adds the job state `interrupted` and `video.resume_format`, `verified_at` and `verified_checks`. Its downgrade turns every `interrupted` job into `cancelled`, because the old code counts a failed job as an attempt.
- Migration `0007` adds `channel.avatar_url` and `job.progress`. An existing channel gets its avatar on the next poll of one of its tab sources.
- `job.progress` holds the current stream of a running archive download: format, percent, downloaded and total bytes, speed and ETA. The download child sends it through a pipe at most once a second. The worker process writes the latest value to the row at most every 5 s, so the child opens no database connection.
- Migration `0005` moves the settings onto `channel` from the rules of the channel's sources, then drops `source.rule_id` and the `rule` table. Where those rules differ, `watch_lives` and `use_cookies` are on when any was on, `date_after` takes the earliest, and `format` comes from the lowest source id.
- The `.info.json` beside each video is the source of truth. A rescan of the tree rebuilds `video` without network calls.
- A playlist is a relation, not a directory. A video sits once under its uploader's channel, and the UI shows the playlist view.

## Layout

New dataset `tank/youtube`, mounted read-write in Talos. Inside it:

```
<channel name> [<channel id>]/
  videos/  <YYYY-MM-DD> <title> [<video id>]/
  shorts/  <YYYY-MM-DD> <title> [<video id>]/
  lives/   <YYYY-MM-DD> <title> [<video id>]/
  members/ <YYYY-MM-DD> <title> [<video id>]/
.incoming/<video id>/   archive downloads
.incoming/<job id>/     recordings
```

- Each video directory holds the media, `.info.json`, the thumbnail, subtitles, and the chat replay when YouTube offers one.
- Type rule, first match wins: `availability == subscriber_only` is `members`. A `was_live` or recorded live is `lives`. A video from the `/shorts` tab is `shorts`. The rest is `videos`.
- The channel directory name is fixed at first add and stored in `channel`. A later channel rename changes the display name only, never the path.
- Downloads land in `.incoming/` on the same dataset, so the final move is a rename and a half-written file never appears in the tree. An archive download's directory is per video, so a retry resumes its parts (archive.md, Resuming and verifying downloads).
- Titles pass through yt-dlp's filename sanitising and a length cap, because a title can exceed the 255-byte name limit in UTF-8.
- Format: `bv*+ba/b`, merged to `mkv`, which holds any codec pair YouTube serves.
- Files inside a video directory are named `<video id>.<ext>`, so only directory names carry a title and need the byte cap.
- yt-dlp's `.tmp` fragments directory is deleted before the rename. The cookie copy lives in a private temporary directory outside `.incoming/`, so it never moves into the tree.
