# Videos

The Videos page lists every stored and known video in one table. A user narrows it by channel or video codec, and opens a video from its row.

## Sub-features

- `videos-list` shows one row per video with a link to its page.
- `videos-channel` filters by channel.
- `videos-codec` filters by the stored video codec.
- `videos-open` opens the video page from a cell outside the title link.

## How to get to it (user POV)

- Choose `Videos` in the sidebar.
- Open `/videos`, with `?channel_name=<name>` or `?vcodec=<codec>` holding a filter.

## Driving it with pw

Preconditions:

- A browser session is open: `pw open "$base/" --browser=chrome`.

- **Sidebar entry.** Choose `Videos`. Run `pw click "role=link[name='Videos']"`. The page URL ends in `/videos`.
- **Baseline.** Run `pw snapshot "role=table" --filename=videos/1-all.yml`. The table holds links to `/videos/e2ePlay0001` and `/videos/e2eOther001`.
- **Channel filter.** Open `Channel` and choose `E2E Other`. Run `pw click "role=combobox[name='Channel']"` and `pw click "role=option[name='E2E Other']"`. The page URL holds `channel_name=E2E+Other`.
- **Channel result.** Run `pw snapshot "role=table" --filename=videos/2-channel.yml`. The first column header reads `Title (1)` and the one row is "Other channel upload".
- **Codec filter.** Load the unfiltered list, open `Video codec` and choose `H.264`. Run `pw goto "$base/videos"`, `pw click "role=combobox[name='Video codec']"` and `pw click "role=option[name='H.264']"`. The page URL holds `vcodec=h264`.
- **Codec result.** Run `pw snapshot "role=table" --filename=videos/3-codec.yml`. The one row is "Playable smoke video" with the cells `H.264 AAC` and `1080p30`.
- **Open from the row.** Choose the resolution cell. Run `pw click "role=cell[name='1080p30']"`. The page URL ends in `/videos/e2ePlay0001`.
- **Proof.** Run `pw screenshot --full-page --filename=videos/4-opened.png`.

## Gotchas

- Only `e2ePlay0001` has stored stream facts. Every other seeded row shows `-` for codec and resolution, so the codec filter always lists one video.
- The filters are read-only. This recipe runs again on the same instance.
