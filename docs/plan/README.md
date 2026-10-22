# savetube plan

Draft for archiving chosen YouTube channels and playlists, recording lives from their first second, and managing both from one web UI. Built: web with login, source polling, live detection, the archive worker, the storage pipeline, the image. Live recording is not built.

## Files

Read this file, then only the files your task touches.

| File | Read when the task touches |
| --- | --- |
| `background.md` | tool survey, yt-dlp behaviour the design rests on, why one custom app |
| `runtime.md` | the web, scheduler, worker and recorder roles, how yt-dlp runs in-process |
| `frontend.md` | sidebar, pages, modals, adding a channel and its API, dashboard statistics |
| `storage.md` | tables and migrations, the directory tree on `tank/youtube`, staging and batch moves |
| `archive.md` | polling sources, playlists, title words, pausing downloads, download limits, resuming and verifying downloads |
| `live.md` | live detection, recording, the VOD |
| `access.md` | login through Zitadel, YouTube cookies |
| `operations.md` | Discord notifications, metrics, build, release, Kubernetes deployment |

## Requirements

1. Back up chosen playlists and whole channels: videos, Shorts, and past lives, at the best quality YouTube serves.
2. Download members-only content with the cookies of an account that is a member of the channel.
3. Watch chosen channels and record a live the moment it starts. An unarchived live is gone once it ends.
4. One web UI to add channels and playlists, choose per channel what to keep, browse stored videos and their metadata, and see storage statistics.
5. A structured tree on `tank`: `<channel>/<type>/<video>`, never a flat directory.
6. Runs in Kubernetes. Download work uses published libraries, not hand-written extractors.

"Original quality" means the best stream YouTube serves. YouTube never serves the uploader's source file.

## Unsettled

- Whether `statfs` through virtiofs reports the dataset's free space or the pool's.
- Which Google account holds the memberships. Using the main account puts it at ban risk. A separate account needs its own memberships.
- Whether members-only uploads and lives appear in RSS and `videos.list`. If not, the `/live` poll and the members playlist need cookies. The `UUMO` playlist-id prefix for members-only uploads is third-party lore, not checked.
- Whether issue 15274's fix (PR 16771) is in a release.
- A read-only Samba mount of `tank/youtube` for watching on a PC. Samba stays a reader, so the dataset keeps one writer.
- Live chat capture during a live, not only replay. Needs a separate library. Not surveyed.
- Offsite backup: none of the current tiers can carry this size.

## Build order

1. Prove an image with Python, Deno, ffmpeg, yt-dlp and the bgutil plugin runs one from-start capture of a public live in the cluster through `YoutubeDL`.
2. Prove a members-only download and a members-only live with the chosen account's cookies.
3. Build the recorder Job and the scheduler's live detection, with Discord notifications. Live recording is the requirement nothing else covers.
4. Build the archive worker, the queue, and the tree layout.
5. Build the UI from `dashboard-01`: statistics, channels, live, settings, the video browser.
