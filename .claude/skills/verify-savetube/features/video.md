# Video page

The video page plays a stored video and shows its format watch: the stored streams, the formats YouTube offers, and a pick that queues a replace of the stored audio or video stream.

## Sub-features

- `video-play` loads the stored file in the player.
- `video-formats` lists stored and offered formats, collapsed until opened.
- `video-replace` queues a replace job for a picked format.

## How to get to it (user POV)

- Choose a video title, or a cell of its row, on the Videos page.
- Choose a video title on the Downloads page.
- Open `/videos/<video id>`.

## Driving it with pw

Preconditions:

- A fresh seed: `e2ePlay0001` has no queued replace.
- A browser session is open: `pw open "$base/" --browser=chrome`.

- **Open.** Run `pw goto "$base/videos/e2ePlay0001"`. The page URL ends in `/videos/e2ePlay0001`.
- **Playback.** Run `pw eval "() => { const v = document.querySelector('main video'); return {src: v.currentSrc, ready: v.readyState, duration: v.duration}; }" > "$RUN/evidence/video/1-player.txt"`. `src` ends in `/api/videos/e2ePlay0001/media`, `ready` is `4` and `duration` is about `2`.
- **Media request.** Run `pw requests --static --filter="/api/videos/e2ePlay0001/media" > "$RUN/evidence/video/2-media-request.txt"`. The file holds a `[GET]` line with `[206]` or `[200]`.
- **Open formats.** Choose `Formats` in the format watch card. Run `pw click "#format-watch >> role=button[name='Formats']"`, then `pw snapshot "#format-watch" --filename=video/3-formats.yml`. The file holds the tables `Video formats` and `Audio formats`, the checked radios `Keep stored video` and `Keep stored audio`, and a disabled `Replace` button.
- **Pick a format.** Choose format 251 in the audio table. Run `pw click "role=radio[name='Select format 251']"`. The card shows an enabled button `Replace with 251`.
- **Replace.** Choose `Replace with 251`. Run `pw click "role=button[name='Replace with 251']"`, then `pw requests --filter="/api/videos/e2ePlay0001/formats/pick" > "$RUN/evidence/video/4-pick-request.txt"`. The file holds one `[POST]` line with `[200]`.
- **After replace.** Run `pw snapshot "#format-watch" --filename=video/5-after-pick.yml`. The card summary reads `replace queued` and the `Replace` button is disabled.
- **Second view.** Open the Downloads page. Run `pw click "role=link[name='Downloads']"` and `pw snapshot "role=main" --filename=video/6-downloads.yml`. The queued table holds `replace 251` in the "Playable smoke video" row.
- **Stored value.** Run `curl -fsS "$base/api/jobs?state=queued" > "$RUN/evidence/video/7-queued.json"`. One job has `"video_id": "e2ePlay0001"`, `"kind": "replace"` and `"format_id": "251"`.
- **Proof.** Run `pw screenshot --full-page --filename=video/8-downloads.png`.

## Gotchas

- Create `$RUN/evidence/video` before the first `>` redirect. `pw --filename` creates it, a shell redirect does not.
- The page logs one console error for the `i.ytimg.com` thumbnail of a seeded id.
- Without ffmpeg at seed time the video has no file: `seed.json` in `$RUN/work` reads `"media": false` and `video-play` cannot pass.
- The replace job stays queued, because no worker runs. The file on disk never changes in this instance.
- `Probe now` has no recipe.
