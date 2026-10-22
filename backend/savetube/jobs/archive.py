"""One archive download: resume, format fallback, and verification."""

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube import integrity
from savetube.jobs.rows import _pin
from savetube.jobs.runners import DownloadFailed, Report, Runner
from savetube.logs import emit, human_bytes
from savetube.storage import dir_size
from savetube.ytdl import exclude_formats, format_server_error, video_url

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# yt-dlp's text when a pinned format is no longer offered.
FORMAT_GONE = "Requested format is not available"
# Formats one attempt excludes after googlevideo refused each before its first
# byte. A video offers a few formats near each quality, and more refusals in a
# row point at the host rather than one format.
MAX_FORMAT_FALLBACKS = 3


def _present(workdir: Path) -> int | None:
    """The bytes already in the working directory, None when it was missing.
    Creates it either way."""
    found = dir_size(workdir) if workdir.exists() else None
    workdir.mkdir(parents=True, exist_ok=True)
    return found


def _part_sizes(workdir: Path) -> dict[Path, int]:
    """yt-dlp's unfinished files: `.part`, and an HLS download's fragments,
    `<name>.part-Frag<n>` until each is appended to its `.part`."""
    return {f: f.stat().st_size for f in workdir.rglob("*.part*") if f.is_file()}


def _refused(
    e: DownloadFailed,
    chosen: str | None,
    streams: list[dict[str, Any]],
    parts: dict[Path, int],
    workdir: Path,
) -> str | None:
    """The format id googlevideo refused with HTTP 500 before the run added a
    byte to it, or None. An HLS stream whose fragments all failed ends on an
    empty file, which yt-dlp deletes. yt-dlp downloads the streams of
    `chosen`, such as `299+251`, in that order, so the failing one is the
    first that sent no `stream` event. Only the failing stream's `.part`
    files can have grown since `parts`, because a finished stream loses its
    `.part` suffix."""
    if chosen is None or not format_server_error(str(e)):
        return None
    if any(size > parts.get(f, 0) for f, size in _part_sizes(workdir).items()):
        return None
    done = {s.get("format_id") for s in streams}
    return next((f for f in chosen.split("+") if f not in done), None)


def _part_bytes(workdir: Path) -> int:
    """The bytes in yt-dlp's unfinished `.part` files, which a resume continues."""
    return sum(f.stat().st_size for f in workdir.rglob("*.part") if f.is_file())


async def _start_over(sessionmaker: async_sessionmaker, video_id: str, workdir: Path) -> None:
    """Delete the parts and the pinned format, leaving an empty directory."""
    shutil.rmtree(workdir, ignore_errors=True)
    _present(workdir)
    async with sessionmaker() as session:
        await _pin(session, video_id, None)
        await session.commit()


def _media_path(info: dict[str, Any]) -> Path | None:
    downloads = info.get("requested_downloads") or []
    path = (downloads[-1] if downloads else info).get("filepath")
    return Path(path) if path else None


async def _archive(
    sessionmaker: async_sessionmaker,
    video_id: str,
    workdir: Path,
    *,
    runner: Runner,
    params: Callable[[str], dict[str, Any]],
    fmt: str,
    pinned: str | None,
    report: Report,
    step: Callable[[str], Awaitable[None]],
    streams: list[dict[str, Any]],
    tools: integrity.Tools,
) -> tuple[dict[str, Any], list[str]]:
    """Download into `workdir`, resuming its parts, and verify the result.
    Returns the info dict and the checks that passed.

    A download that fails a check starts over from zero once, within the same
    attempt. A second failure raises DownloadFailed.

    A format googlevideo refuses before its first byte is excluded from the
    channel's selector, and the download runs again, up to
    MAX_FORMAT_FALLBACKS times per attempt.
    """
    url = video_url(video_id)
    excluded: list[str] = []

    async def fetch(pin: str | None = None) -> dict[str, Any]:
        """One download with `pin`, else the channel's selector, again with
        the format googlevideo refused excluded (_refused)."""
        selector = pin or exclude_formats(fmt, excluded)
        chosen: str | None = None
        # The format refused last, logged with the one the next run picks.
        pending: str | None = None

        def fallback(format_id: str | None) -> None:
            nonlocal pending
            if pending is not None:
                emit(
                    log,
                    logging.WARNING,
                    "download.fallback",
                    excluded=pending,
                    format=format_id,
                    reason="HTTP Error 500",
                )
                pending = None

        async def seen(message: dict[str, Any]) -> None:
            nonlocal chosen
            if message.get("event") == "format":
                chosen = message["format_id"]
                fallback(chosen)
            await report(message)

        while True:
            streams.clear()
            chosen = None
            parts = _part_sizes(workdir)
            try:
                return await runner(url, params(selector), seen)
            except DownloadFailed as e:
                fallback(None)
                failed = _refused(e, chosen, streams, parts, workdir)
                if failed is None or len(excluded) >= MAX_FORMAT_FALLBACKS:
                    raise
                excluded.append(failed)
                pending = failed
                selector = exclude_formats(fmt, excluded)

    for fresh in (False, True):
        streams.clear()
        present = _present(workdir)
        resumed = pinned is not None and present is not None
        if pinned is not None and not resumed:
            await _start_over(sessionmaker, video_id, workdir)
            pinned = None
        if resumed:
            emit(
                log,
                logging.INFO,
                "download.resume",
                format=pinned,
                present=human_bytes(present),
                part=human_bytes(_part_bytes(workdir)),
            )
            try:
                info = await fetch(pinned)
            except DownloadFailed as e:
                if FORMAT_GONE not in str(e):
                    raise
                emit(
                    log,
                    logging.WARNING,
                    "download.restart",
                    format=pinned,
                    present=human_bytes(dir_size(workdir)),
                    reason="the pinned format is no longer offered",
                )
                await _start_over(sessionmaker, video_id, workdir)
                streams.clear()
                resumed = False
                info = await fetch()
        else:
            info = await fetch()

        media = _media_path(info) or workdir / "missing"
        await step("verifying")
        try:
            checks = await asyncio.to_thread(
                integrity.verify, media, info, list(streams), resumed=resumed, tools=tools
            )
        except integrity.IntegrityFailed as e:
            emit(
                log,
                logging.WARNING,
                "download.integrity_failed",
                check=e.check,
                **e.numbers,
                resumed="yes" if resumed else "no",
                then="fail" if fresh else "start over",
            )
            await _start_over(sessionmaker, video_id, workdir)
            pinned = None
            if fresh:
                raise DownloadFailed(f"integrity check failed: {e}") from None
            continue
        emit(log, logging.INFO, "download.verified", checks=",".join(checks) or "none")
        return info, checks
    raise AssertionError("unreachable")
