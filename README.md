# savetube

Self-hosted YouTube archiver. It backs up chosen channels and playlists, records lives from their first second, and shows the archive and its statistics in one web UI. Built on yt-dlp.

A scaffold: login, the read API, the schema, the storage pipeline, the image and the Helm chart exist. Live detection and source polling do not. The design is in `docs/plan.md`.

## Layout

- `backend/` is the Python project: `pyproject.toml`, `uv.lock`, the `savetube` package and its tests.
- `frontend/` is the Vite app. The image serves its build as static files.
- The root holds what spans both: `Dockerfile`, `compose.yaml`, `Makefile`, `.env`, `charts/`, `docs/`.

## Develop

1. `cp .env.sample .env` and fill it in. The database is the shared dev server.
2. `uv sync --project backend`, then `make check` and `make lint`. `make check` needs `SAVETUBE_TEST_DATABASE_URL` and the `helm` binary.
3. `make web` serves the API on `127.0.0.1:8000`. `cd frontend && pnpm dev` serves the UI on `127.0.0.1:5173` and proxies `/api` and `/auth` to it.

`docker compose up --build` runs the image with every role, on OrbStack or Docker, against the database in `.env`.

## Deploy

`charts/savetube/` is the Helm chart. It expects existing Secrets for the database URL, the session key and the cookie file, and an existing PVC for the archive. See `charts/savetube/values.yaml`.
