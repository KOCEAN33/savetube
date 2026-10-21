# Frontend

## Frontend

- React with shadcn/ui, starting from the `dashboard-01` block: sidebar, section cards, an interactive area chart, and a data table. `npx shadcn add dashboard-01` installs it.
- Vite builds a static bundle that FastAPI serves as static files. The block ships as Next.js page files, so its components move into a Vite app.
- Sidebar: Dashboard, Channels, Live, Settings, Videos. Videos stays as built and gets its own review later.
- Live lists record jobs: detected, upcoming and recorded streams. Archive download jobs show on their channel's page.
- Settings holds the download limits and the global title words.
- Every create and edit form opens in a large modal in the middle of the screen, never a side sheet.

## Adding a channel

One modal takes a channel from URL to downloading. No setting needs to exist first.

1. The user pastes a channel URL. A first scan lists the first 30 entries of the Videos, Shorts and Live tabs and the channel's playlists. It does not list the whole channel.
2. The user picks what to archive: each tab, all or some playlists, and whether to record lives.
3. The user adds include and exclude title words. The scanned titles show which ones the words keep and which they filter out, updated as the words change. Word suggestions come from the scanned titles.
4. The user sets the date cutoff, quality and cookie use, and saves. Saving creates the channel and its sources with `archiving` on, so downloads start on the next scheduler tick.

The channel page edits the same settings later: tabs, playlists, live recording, title words, date cutoff, quality, cookie use, poll interval, and pausing downloads (`archiving`).

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /api/channel-scan` | `{url}`, a channel URL | channel id, handle and name, the first-page entries `{video_id, title}` of each tab, and the playlists `{id, title, url}`. Listed live through yt-dlp, 502 on a yt-dlp error, 504 after 120 s |
| `POST /api/title-preview` | `{channel_id?, titles, include_words, exclude_words}` | each title with `kept`, matched with the stored global lists and, when `channel_id` names a stored channel, its lists merged in |
| `POST /api/channels` | the scan's channel URL, `channel_id`, `handle` and `name`, chosen tabs, chosen playlists or all playlists, `watch_lives`, words, `date_after`, `format`, `use_cookies` | the channel with its sources |

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
