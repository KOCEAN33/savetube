# Access

## Login

- The backend is the OpenID Connect client against Zitadel at `https://auth.hiriri.com`: authorization code with PKCE (S256). The browser holds only a signed session cookie with `sub`, name, email and `groups`. No token reaches the frontend.
- A client secret is optional. Unset means a public client, a Zitadel Web application with authentication method `None`.
- Zitadel project `savetube`, application `savetube`, role `savetube-admin`. A project of its own, because savetube writes `tank/youtube` and an `internal` grant must not pass its login (homelab-platform `docs/zitadel-oidc-setup.md`).
- `SAVETUBE_OIDC_REQUIRED_GROUP` checks the flat `groups` claim the `groupsClaim` Action writes, read from the userinfo endpoint. A user without the group gets 403 and no session.
- Redirect URI `https://savetube.lab.hiriri.com/auth/callback`. Post Logout URI `https://savetube.lab.hiriri.com/`. Logout is a POST that clears the session and sends the browser to `end_session` with `client_id`.
- Redirect URIs and the cookie's Secure flag come from `SAVETUBE_PUBLIC_URL`. Uvicorn runs with proxy headers off, so no forwarded header is trusted.
- `/api/*` answers 401 without a session. `/healthz`, the static bundle, and the metrics port need none.
- Without a session the frontend shows a login wall in place of the app. Any 401 refetches `/api/me`, and its 401 shows the wall. A login starts only from the wall's button: each `/auth/login` rewrites the session cookie's state, so logins that start on their own, one per tab, break each other's callback.
- A failed callback redirects to `/?auth_error=<error>`, and the wall shows the error. A session that is already signed in lands on the app. `auth.failed` logs `cookie` (the session cookie arrived), `states` (pending logins in it), `signed_in` and `agent`, which tell a lost cookie from an overwritten one.

## Cookies and account

- A cookie profile is one YouTube account's Netscape cookie file. The chart mounts one Secret as the directory `SAVETUBE_COOKIES_DIR`, and each key `<name>.txt` is the profile `<name>`. Only the scheduler, worker and recorder pods mount it. The internet-facing web pod never does.
- The scheduler records the present profile names in the `cookie_profile` table every minute. The web lists the profiles and checks the general profile choice against that table. The Secret comes from 1Password through an ExternalSecret (proxmox-talos-lab ADR secrets).
- Each run copies the profile's file into a private temporary directory, because yt-dlp writes the cookie jar back and the mount is read-only.
- The scheduler reads which YouTube account each recorded profile's file signs in, so a wrong account in a file shows in the UI. A profile name says nothing about its account. The read loads the YouTube home page and the `account/account_menu` endpoint with the file, and stores the account name, the handle, the file's sha256 and the read time on `cookie_profile`.
- A read runs only when the file's sha256 differs from the stored one, so one file content costs one read. It runs in its own loop every minute, one profile at a time, not while a YouTube block pauses downloads, and stops for the tick after a bot check.
- A failed read clears the name, the handle and the sha256, and logs `cookies.account_failed`. A logged-out file fails as `not signed in`. A failed profile retries after 1 h, not every minute.
- A file whose sha256 differs from a stored one, a replaced file, deletes the profile's `membership` rows in the same transaction as the read result, and logs `cookies.changed` with the deleted count. The next poll that lists a members-only video then checks the new account at once, not after 24 h. A first read, or the first read after a failed one, has no stored sha256 and deletes nothing.
- Two uses, kept apart so the user's real account sees as few requests as possible:
  - The general profile, one global choice in Settings (`download_settings.cookie_profile`, null for cookie-free), serves every poll, every live probe, and every download that is not members-only. It is meant for a throwaway account. A bot check costs that account at worst.
  - A members-only video downloads with a profile whose membership in the video's channel is confirmed. No user choice is involved. A profile never serves a general request unless it is the general profile.
- Membership is checked automatically, per channel and profile. The check reads one members-only video id that a poll listed for the channel, with yt-dlp's info extraction and no download. Formats mean a member. A members-only error means not a member. Any other error, a bot check included, keeps the previous result and logs `membership.check_failed`.
- A result lives in `membership` (channel, profile, `is_member`, `checked_at`, the probed video id). A poll that lists members-only entries checks every profile whose result is missing or older than 24 h. Checks run after the poll, one at a time, and not while a YouTube block pauses downloads.
- A members-only entry passes a poll only when a profile is a confirmed member of its channel. Otherwise it is skipped as `members`. A listed video stored as members-only counts as a members-only entry, because the Shorts tab lists a members-only short without its availability. If two profiles are members, the one first by name downloads. A channel's `members_all` switch exempts its members-only entries from the title words, never from this check (archive.md, Title words).
- A members-only live recording uses the same member profile.
- A download or recording that the general profile fails with a members-only error stores the video as members-only. It then runs once more in the same job with the channel's member profile, if one exists. Without one the job fails, and the next poll checks the channel's memberships with that video.
- Live detection probes with the general profile. When that probe fails with a members-only error, it probes once more with the channel's member profile, if one exists, and stores the video as members-only.
- The channel page shows each profile's membership result and its check time. Settings lists the profiles and picks the general one.
- Export cookies from a private window that is then closed, because YouTube rotates cookies on open tabs.
- Requests run with the `-t sleep` intervals and a global concurrency cap across `worker` and recorders.
