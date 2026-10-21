# Frontend

## Frontend

- React with shadcn/ui, starting from the `dashboard-01` block: sidebar, section cards, an interactive area chart, and a data table. `npx shadcn add dashboard-01` installs it.
- Vite builds a static bundle that FastAPI serves as static files. The block ships as Next.js page files, so its components move into a Vite app.
- Sidebar: Dashboard, Channels, Live, Settings, Videos. Videos stays as built and gets its own review later.
- Live lists record jobs: detected, upcoming and recorded streams. Archive download jobs show on their channel's page.
- Settings holds the download limits, the general cookie profile, and the global title words. The profile select offers "None (cookie-free)" and each present profile from `GET /api/cookie-profiles`.
- The site header holds the pause switch for every archive download (archive.md, Pausing downloads), on every page. While paused it shows a "Downloads paused" badge and a Resume button. "Pause downloads" opens a confirmation modal first, because a pause stops the running downloads. Resume applies at once. Settings shows the same switch above the download limits.
- Every create and edit form opens in a large modal in the middle of the screen, never a side sheet.

## Adding a channel

One modal takes a channel from URL to downloading. No setting needs to exist first.

1. The user pastes a channel URL. A first scan lists the first 30 entries of the Videos, Shorts and Live tabs and the channel's playlists. It does not list the whole channel.
2. The user picks what to archive: each tab, all or some playlists, and whether to record lives. With playlists chosen, the checkbox "Also download videos other channels uploaded to these playlists" sets `other_channels` on the playlist sources. It starts checked.
3. The user adds include and exclude title words. The scanned titles show which ones the words keep and which they filter out, updated as the words change. Word suggestions come from the scanned titles.
4. The user sets the date cutoff and quality, and saves. Saving creates the channel and its sources with `archiving` on, so downloads start on the next scheduler tick.

The channel page edits the same settings later: tabs, playlists, live recording, title words, date cutoff, quality, poll interval, and pausing downloads (`archiving`).

## Channel page

- The Channels page lists only channels with `tracked` true, which `GET /api/channels` sets when at least one source names the channel. A channel stored only as the uploader of another channel's playlist video stays in `channel`, and the video pages and the Videos page channel filter still reach it.
- The channel list, the channel page header and the scan step of the add-channel modal show the channel's avatar. The browser loads it from YouTube's image host, and the first letter of the name stands in until it loads or while no avatar is stored.
- "Download settings" shows each cookie profile's membership result for the channel and its check time, read-only. A membership comes only from the automatic check.
- "What to archive", "Title words" and "Download settings" fold under their title. Each starts folded, and the browser keeps each section's open state in `localStorage`, shared by every channel page.
- The Playlists area of "What to archive" shows the `other_channels` checkbox while the channel has a playlist source. It reads every top-level `playlist` and `playlists` source. When their values differ, it shows as mixed with a count, and a change sets every one. A `playlists` parent copies the value to its children. A playlist source added from the channel page takes the current value, and on when the channel has no playlist source yet.
- Under both checkboxes, a line states that a video downloads once, sits under its uploader's channel folder, and shows on this channel's page.
- "Downloads" shows the running archive download first: its title and the current stream's percent, downloaded and total bytes, speed and ETA. The queued jobs follow, oldest first, which is the order the dispatcher takes them. The latest succeeded, failed, cancelled and interrupted jobs come last. The queued and finished lists each show 10 jobs, and "Show more" adds 10.
- The downloads refetch every 3 s while a download runs, and every 15 s otherwise. The worker writes progress every 5 s, so a faster refetch shows nothing new.
- A download shows no progress while yt-dlp fetches the metadata, subtitles and chat replay, because the progress counts the media only.
- "Stored videos" lists the channel's complete videos in one group per type (videos, shorts, lives, members), newest upload first, 12 at a time. Each card shows the thumbnail, title, upload date, duration and size, and links to the video page.
- A video counts as the channel's when the channel uploaded it or one of its sources lists it. A collaboration on the channel's tab is stored under the uploader its info names, so the uploader alone misses it.
- The channel list, the channel page header and "Stored videos" count complete videos by that rule. "Browse videos" and "Open in Videos" open the Videos page with `listed_by`, which applies the same rule.
- Thumbnails load from `i.ytimg.com`, because the web pod does not mount the archive.

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /api/channel-scan` | `{url}`, a channel URL | channel id, handle, name and `avatar_url`, the first-page entries `{video_id, title}` of each tab, and the playlists `{id, title, url}`. Listed live through yt-dlp, 502 on a yt-dlp error, 504 after 120 s |
| `POST /api/title-preview` | `{channel_id?, titles, include_words, exclude_words}` | each title with `kept`, matched with the stored global lists and, when `channel_id` names a stored channel, its lists merged in |
| `POST /api/channels` | the scan's channel URL, `channel_id`, `handle`, `name` and `avatar_url`, chosen tabs, chosen playlists or all playlists, `other_channels` (default true, applied to playlist sources only), `watch_lives`, words, `date_after`, `format` | the channel with its sources |
| `GET /api/jobs` | `kind`, `state` (repeat it for several states), `channel_id`, `limit` | jobs, newest first. `channel_id` matches the jobs of videos the channel uploaded or its sources list. A running download carries `progress` `{format, pct, done, total, speed, eta}`, and `pct` is null without a total |
| `GET /api/videos` | `channel_id` (the uploader), `listed_by` (the uploader or a channel whose sources list the video), `type`, `state`, `q`, `limit`, `offset` | `{total, items}`, newest upload first |

## Statistics

The dashboard reads Postgres. No second metrics store backs it.

| Widget | Source |
| --- | --- |
| Total stored size, video count, hours archived | `sum(size_bytes)`, `count(*)`, `sum(duration)` over `video` |
| Size and count by channel and by type | the same, grouped by `channel_id`, `type` |
| Bytes added per day or week | `video.completed_at` bucketed with `date_trunc` |
| Live recordings: recorded, failed, missed | `job` where kind is record, grouped by state |
| Queue: pending, running, failed | procrastinate's job table |
| Dataset free space | `statfs` on the mount, sampled into a table hourly |
| Last sync per source | `source.last_polled_at`, `source.last_error` |

- `video.size_bytes` sums every file in the video's directory, so subtitles and thumbnails count.
- A missed live is a detected stream whose recorder never captured from the first segment. It carries the reason: DVR off, ended before detection, or recorder failure.
