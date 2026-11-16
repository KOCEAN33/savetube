# Settings

The Settings page holds the global title words, the default subtitle language, and the download limits. This file maps the download limits form: a user changes a limit and saves it.

## Sub-features

- `settings-limits-read` shows the stored limits in the form.
- `settings-limits-save` stores a changed limit.

## How to get to it (user POV)

- Choose `Settings` in the sidebar.
- Open `/settings`.

## Driving it with pw

Preconditions:

- A fresh seed: max concurrent downloads is 2.
- A browser session is open: `pw open "$base/" --browser=chrome`.

- **Sidebar entry.** Choose `Settings`. Run `pw click "role=link[name='Settings']"`. The page URL ends in `/settings`.
- **Baseline.** Run `pw snapshot "form:has(#dl-concurrent)" --filename=settings/1-before.yml`. `Max concurrent downloads` reads `2`, `Rate limit (MB/s)` reads `5`, and the daily limit reads `20`.
- **Change.** Type 3 into `Max concurrent downloads`. Run `pw fill "#dl-concurrent" "3"`.
- **Save.** Choose `Save` in that form. Run `pw click "form:has(#dl-concurrent) >> button[type=submit]"`, then `pw requests --filter="/api/download-settings" > "$RUN/evidence/settings/2-put-request.txt"`. The file holds a `[PUT]` line with `[200]`.
- **Second view.** Load the page again. Run `pw reload` and `pw snapshot "form:has(#dl-concurrent)" --filename=settings/3-after.yml`. `Max concurrent downloads` reads `3`.
- **Stored value.** Run `curl -fsS "$base/api/download-settings" > "$RUN/evidence/settings/4-stored.json"`. The file holds `"max_concurrent":3`.
- **Proof.** Run `pw screenshot --full-page --filename=settings/5-after.png`.

## Gotchas

- The page has two `Save` buttons, one for the title words and one for the limits. Scope the click to the form that holds `#dl-concurrent`.
- Title words, the subtitle language, the window, and the cookie profile have no recipe.
