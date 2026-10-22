# Frontend

## Frontend

- React with shadcn/ui, starting from the `dashboard-01` block: sidebar, section cards, an interactive area chart, and a data table. `npx shadcn add dashboard-01` installs it.
- Vite builds a static bundle that FastAPI serves as static files. The block ships as Next.js page files, so its components move into a Vite app.
- The user chose the library layout: the sidebar lists the channels, each page searches its own lists, and no global search bar exists.
- Sidebar: Home, Live, Videos, Settings, then "Channels (N)". N counts the tracked channels. A + button opens the add-channel modal, and a filter input matches a channel's name or handle. Each tracked channel shows its avatar, its name and its stored-video count, sorted by name, and links to its channel page. No channel list page exists, and `/channels` redirects to `/`.
- The Videos page is the cross-channel library: search and filters across every channel, recent downloads, and channels known only as an uploader. A channel's own videos are browsed on its channel page, so nothing links from a channel into the Videos page.
- The Videos page channel filter lists each channel name once and filters by `channel_name`. A group's member channels, such as KMNZ's per-member channels, each have their own channel id but carry the group's name in their video info, so one entry shows the whole group. Two unrelated channels with the same name also merge there. A tracked channel's name follows its tab listing, which can differ from the name its video info carries and split it from its group.
- Live lists record jobs: detected, upcoming and recorded streams. Archive download jobs show on their channel's page.
- Settings holds the download limits, the general cookie profile, and the global title words. The window has an on/off checkbox beside its times, which stay editable while it is off. The daily limit field shows "n / limit today" from `downloaded_today`. The profile select offers "None (cookie-free)" and each present profile from `GET /api/cookie-profiles`.
- Settings also holds the default subtitle language: Off, ja, en, ko, zh-Hans, zh-Hant, or another code typed in. It applies at once and lives in the browser's `localStorage` (`savetube.subtitle-lang`), not in the database, so each browser keeps its own.
- The site header holds the pause switch for every archive download (archive.md, Pausing downloads), on every page. While paused it shows a "Downloads paused" badge and a Resume button. "Pause downloads" opens a confirmation modal first, because a pause stops the running downloads. Resume applies at once. The switch refetches the flag every 15 s, so a change from another tab shows. Settings shows the same switch above the download limits.
- The sidebar user button opens a menu with only "Log out". The app has one account, so the menu repeats no name or email.
- Every create and edit form opens in a large modal in the middle of the screen, never a side sheet.

## Home

- A "Sources waiting" card sits at the top while at least one source has no channel. A source gets its channel on its first successful poll, so the card lists sources before that poll and sources whose first poll failed, with the shortened `last_error` and a "Poll now" button. A disabled source shows "Disabled, not polled" and no button, because the scheduler skips it. The card refetches every 15 s and refreshes the sidebar channel list when a source leaves it.
- A "Downloads" card shows archive downloads across every channel. Production holds many channels, so one channel's page cannot show them all.
- The card's header counts the running and the queued jobs from `GET /api/jobs/counts`. While videos wait on staging, a second line gives their count and size from `GET /api/staging` (storage.md, Staging), with a Move now button. After a press the line says the move starts within a minute, and the button stays disabled until the scheduler takes the request. Each running job shows its uploader channel, the video title linked to the video page, and the progress the channel page shows.
- "Recently failed and interrupted" lists the latest 10 `failed` and `interrupted` archive runs, and "Show more" adds 10. Each shows the channel, the title, the finish time and the job's `reason`. Opening the reason shows the full `log_tail`.
- The card refetches every 3 s while a download runs, and every 15 s otherwise.
- The order is the statistics cards, the chart, the Downloads card, then the per-channel table. Downloads still shows while the statistics load or fail.
- The per-channel table lists tracked channels only and counts a channel's videos by the channel page's rule (Channel page). A video two tracked channels claim counts under both, so the shares can sum past 100%.
- A row of the per-channel table opens the channel page. A row of the per-type table opens the Videos page filtered to that type.

## Adding a channel

One modal takes a channel from URL to downloading. No setting needs to exist first.

1. The user pastes a channel URL. A first scan lists the first 30 entries of the Videos, Shorts and Live tabs and the channel's playlists. It does not list the whole channel.
2. The user picks what to archive: each tab, all or some playlists, and whether to record lives. With playlists chosen, the checkbox "Also download videos other channels uploaded to these playlists" sets `other_channels` on the playlist sources. It starts checked.
3. The user adds include and exclude title words. The scanned titles show which ones the words keep and which they filter out, updated as the words change. Word suggestions come from the scanned titles.
4. The user sets the date cutoff and quality, and saves. The summary lists the choices, and with playlists chosen it states whether videos other channels uploaded download. Saving creates the channel and its sources with `archiving` on, so downloads start on the next scheduler tick.

The channel page edits the same settings later: tabs, playlists, live recording, title words, date cutoff, quality, poll interval, and pausing downloads (`archiving`).

## Channel page

- The sidebar lists only channels with `tracked` true, which `GET /api/channels` sets when at least one source names the channel. A channel stored only as the uploader of another channel's playlist video stays in `channel`, and the video pages and the Videos page channel filter still reach it.
- A header row spans the page: avatar, name, handle linked to the channel on YouTube, the stats inline (stored, size, sources, last poll), and badges for recording lives, paused, and failing sources. The site header shows the channel name.
- Below it, segmented tabs with counts: Videos, Shorts, Live, Members, Playlists, Downloads. `?tab=` holds the tab, and Videos is the default. Members shows only while the channel has a complete members-only video. The user has not decided where members-only videos belong, so this tab is the minimal placement.
- Videos, Live and Members show rows: thumbnail, title linked to the video page, upload date, duration, size. Shorts show 9:16 tiles. Each tab has its own search box, and one newest/oldest sort serves every tab. A list loads 30 videos, and "Show more" adds 30.
- Playlists lists each `playlist` source, picked or under "all playlists", as a row that folds out to its stored videos from `GET /api/videos?source_id=`. The search box there filters the playlist titles.
- Downloads holds "What to archive", "Title words", "Download settings" with the membership list, and the job lists.
- The sidebar, the channel page header and the scan step of the add-channel modal show the channel's avatar. The browser loads it from YouTube's image host, and the first letter of the name stands in until it loads or while no avatar is stored.
- "Download settings" shows each cookie profile's membership result for the channel and its check time, read-only. A membership comes only from the automatic check.
- In Downloads, "What to archive", "Title words" and "Download settings" fold under their title. Each starts folded, and the browser keeps each section's open state in `localStorage`, shared by every channel page.
- The Playlists area of "What to archive" always shows the `other_channels` checkbox. While the channel has a playlist source, it reads every top-level `playlist` and `playlists` source. When their values differ, it shows as mixed with a count, and a change sets every one. A `playlists` parent copies the value to its children. A playlist source added from the channel page takes the current value, and a mixed value counts as on.
- While the channel has no playlist source, the checkbox holds the value for the first one and starts checked. "Track all playlists" and "Pick playlists" create their sources with it. The value lives in the page, so a reload resets it to checked.
- Under both checkboxes, a line states that a video downloads once, sits under its uploader's channel folder, and shows on this channel's page.
- "Downloads" shows the running archive download first: its title and the current stream's percent, downloaded and total bytes, speed and ETA. The queued jobs follow, oldest first, which is the order the dispatcher takes them. The latest succeeded, failed, cancelled and interrupted jobs come last, and a failed or interrupted row shows its `reason`, with the full `log_tail` on hover. The queued and finished lists each show 10 jobs, and "Show more" adds 10. A job table keeps the title at least 12rem wide and wraps the reason, so a narrow window scrolls the table sideways instead of hiding the title.
- The downloads refetch every 3 s while a download runs, and every 15 s otherwise. The worker writes progress every 5 s, so a faster refetch shows nothing new.
- A download shows no progress while yt-dlp fetches the metadata, subtitles and chat replay, because the progress counts the media only.
- After the last byte, the download shows its step in place of the percent: merging the streams, checking the file, or writing the audio copy. These steps take about 2 minutes on a 7 GiB video.
- A video counts as the channel's when the channel uploaded it or one of its sources lists it. A collaboration on the channel's tab is stored under the uploader its info names, so the uploader alone misses it.
- The sidebar count, the header's stored count and the video tabs count complete videos by that rule, through `listed_by`.
- Thumbnails load from `i.ytimg.com`, because no endpoint serves the stored thumbnail.

| Method and path | Body | Returns |
| --- | --- | --- |
| `POST /api/channel-scan` | `{url}`, a channel URL | channel id, handle, name and `avatar_url`, the first-page entries `{video_id, title}` of each tab, and the playlists `{id, title, url}`. Listed live through yt-dlp, 502 on a yt-dlp error, 504 after 120 s |
| `POST /api/title-preview` | `{channel_id?, titles, include_words, exclude_words}` | each title with `kept`, matched with the stored global lists and, when `channel_id` names a stored channel, its lists merged in |
| `POST /api/channels` | the scan's channel URL, `channel_id`, `handle`, `name` and `avatar_url`, chosen tabs, chosen playlists or all playlists, `other_channels` (default true, applied to playlist sources only), `watch_lives`, words, `date_after`, `format` | the channel with its sources |
| `GET /api/jobs` | `kind`, `state` (repeat it for several states), `channel_id`, `limit` | jobs, newest first. `channel_id` matches the jobs of videos the channel uploaded or its sources list. Each job carries the video's `title`, its uploader's `channel_id` and `channel_name`, `log_tail`, and `reason`. A running download carries `progress` `{format, pct, done, total, speed, eta}`, and `pct` is null without a total. After the last byte, `progress` is `{step}`: `merging`, `verifying`, or `audio` |
| `GET /api/jobs/counts` | `kind`, `channel_id` | `{state: count}` with every job state, 0 when no job holds it. `channel_id` matches as in `GET /api/jobs` |
| `GET /api/staging` | | `{count, bytes, move_requested}` of the `staged` videos (storage.md, Staging) |
| `POST /api/staging/move` | | asks the scheduler to move every staged video on its next check. Returns what `GET /api/staging` returns |
| `GET /api/videos` | `channel_id` (the uploader), `channel_name` (uploaders with that stored name), `listed_by` (the uploader or a channel whose sources list the video), `source_id` (videos `video_source` links to the source, or for a `playlists` parent to any of its children), `type`, `state`, `q`, `order` (`newest` or `oldest` upload first, default `newest`, a missing date last), `limit` (at most 200), `offset` | `{total, items}` |
| `GET /api/videos/{id}/media` | | the stored `<id>.<info.ext>`, as `video/x-matroska` for mkv, with HEAD and HTTP Range (206, 416). The path comes from the row's `dir_path` under the archive root only. 404 unless the video is `complete` and the file exists |
| `GET /api/videos/{id}/audio` | | the stored `<id>.opus` audio copy as `audio/ogg`, by the same rules |
| `GET /api/videos/{id}/subtitles` | | `[{lang, label}]` sorted by `lang`, one per `<id>.<lang>.vtt` in the video's directory. The chat replay never appears. `label` is the first `name` in `info.subtitles[lang]`, else the code. 404 like `/media` |
| `GET /api/videos/{id}/subtitles/{lang}` | | that `.vtt` file as `text/vtt; charset=utf-8`. `lang` must equal a code the directory listing yields, so no path is built from it. 404 otherwise |

## Video page

- A complete video's page plays the stored mkv in a `<video>` player with the YouTube thumbnail as poster, from `GET /api/videos/{id}/media`. An "Audio only" player below it plays the Opus copy, and "No audio copy stored." replaces it when the file is missing.
- The player gets one `<track kind="subtitles">` per stored subtitle language, so Chrome's captions menu lists each language and off. The track whose code equals the browser's default subtitle language starts showing. No match, or no default, shows none. Picking a language or off in the captions menu becomes the new default. The chat replay is not shown.
- Chrome plays the mkv as stored: H.264, VP9 and AV1 video with Opus audio, seeking included. Safari is not supported, and no transcode or remux exists. A browser whose `canPlayType("video/x-matroska")` is empty gets a note instead of the player.

## Statistics

Home's statistics read Postgres. No second metrics store backs it.

| Widget | Source |
| --- | --- |
| Total stored size, video count, hours archived | `sum(size_bytes)`, `count(*)`, `sum(duration)` over `video` |
| Size and count by channel and by type | the same, grouped by tracked channel under the channel page's rule, and by `type` |
| Bytes added per day or week | `video.completed_at` bucketed with `date_trunc` |
| Live recordings: recorded, failed, missed | `job` where kind is record, grouped by state |
| Queue: pending, running, failed | procrastinate's job table |
| Dataset free space | `statfs` on the mount, sampled into a table hourly |
| Last sync per source | `source.last_polled_at`, `source.last_error` |

- `reason` is one line the API makes from `log_tail`, with no column behind it. A YouTube block reads "blocked by YouTube", a `NETWORK_MARKERS` match (archive.md) "network error", and a missing member profile "members-only, no member cookie profile". An integrity failure names its check, and a worker stop reads "worker stopped" or "worker shut down". Any other text loses yt-dlp's prefixes and is cut to 120 characters.
- `video.size_bytes` sums every file in the video's directory, so subtitles, thumbnails and the audio copy count.
- A missed live is a detected stream whose recorder never captured from the first segment. It carries the reason: DVR off, ended before detection, or recorder failure.
