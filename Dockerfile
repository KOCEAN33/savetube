# syntax=docker/dockerfile:1
FROM node:26-bookworm-slim AS frontend
WORKDIR /src
# npm, not corepack: corepack may not know the keys that sign newer pnpm releases.
RUN npm install -g pnpm@12.9.1
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

# yt-dlp needs a JS runtime since 2025.11.12, Deno 2.3.0 or later.
FROM denoland/deno:bin-2.9.5 AS deno

# The venv's python links to /usr/local/bin/python3, so this stage and the
# final stage use the same base image and the same /app path.
FROM python:3.14-slim-trixie AS backend
COPY --from=ghcr.io/astral-sh/uv:0.13.2 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY backend/savetube ./savetube
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# glibc, because Deno does not support musl.
FROM python:3.14-slim-trixie
COPY --from=deno /deno /usr/local/bin/deno
# Debian's ffmpeg pulls Mesa and LLVM (about 180 MB) through libavdevice. The
# static builds checked lack codecs Debian has, or are a newer major version.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# --no-editable installs savetube into the venv, so the venv is the whole backend.
COPY --from=backend /app/.venv ./.venv
COPY --from=frontend /src/dist ./static

# HOME and DENO_DIR point at /tmp, the one writable path under readOnlyRootFilesystem.
ENV PATH=/app/.venv/bin:$PATH \
    SAVETUBE_STATIC_DIR=/app/static \
    HOME=/tmp \
    DENO_DIR=/tmp/deno
USER 1000:1000
EXPOSE 8000 9090
ENTRYPOINT ["savetube"]
CMD ["web"]
