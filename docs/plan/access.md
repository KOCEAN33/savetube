# Access

## Login

- The backend is the OpenID Connect client against Zitadel at `https://auth.hiriri.com`: authorization code with PKCE (S256). The browser holds only a signed session cookie with `sub`, name, email and `groups`. No token reaches the frontend.
- A client secret is optional. Unset means a public client, a Zitadel Web application with authentication method `None`.
- Zitadel project `savetube`, application `savetube`, role `savetube-admin`. A project of its own, because savetube writes `tank/youtube` and an `internal` grant must not pass its login (homelab-platform `docs/zitadel-oidc-setup.md`).
- `SAVETUBE_OIDC_REQUIRED_GROUP` checks the flat `groups` claim the `groupsClaim` Action writes, read from the userinfo endpoint. A user without the group gets 403 and no session.
- Redirect URI `https://savetube.lab.hiriri.com/auth/callback`. Post Logout URI `https://savetube.lab.hiriri.com/`. Logout is a POST that clears the session and sends the browser to `end_session` with `client_id`.
- Redirect URIs and the cookie's Secure flag come from `SAVETUBE_PUBLIC_URL`. Uvicorn runs with proxy headers off, so no forwarded header is trusted.
- `/api/*` answers 401 without a session. `/healthz`, the static bundle, and the metrics port need none.

## Cookies and account

- The cookie file is one 1Password item, delivered by an ExternalSecret (proxmox-talos-lab ADR secrets) and copied into an `emptyDir` per run, because yt-dlp writes the cookie jar back.
- Only sources of a channel with "use cookies" on pass the file. Public sources run cookie-free with PO tokens.
- Export cookies from a private window that is then closed, because YouTube rotates cookies on open tabs.
- Requests run with the `-t sleep` intervals and a global concurrency cap across `worker` and recorders.
