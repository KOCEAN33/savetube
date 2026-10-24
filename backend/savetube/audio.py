"""The audio-only copy beside an archived video (docs/plan/archive.md, Audio copy)."""

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube.db.models import Job, JobKind, JobState, Video, VideoState
from savetube.integrity import IntegrityFailed, Tools, verify_recording
from savetube.logs import emit, human_bytes
from savetube.storage import dir_size

log = logging.getLogger(__name__)

# ffprobe's codec name to the file extension and the ffmpeg muxer that writes it.
_FORMATS = {"opus": ("opus", "opus"), "aac": ("m4a", "ipod")}
_FALLBACK = ("mka", "matroska")
SUFFIXES = (".opus", ".m4a", ".mka")


class Skipped(Exception):
    """The file gets no audio copy. The message says why."""


class AudioFailed(Exception):
    pass


def _plan(media: Path, tools: Tools) -> tuple[Path, str]:
    try:
        probed = tools.probe(media)
    except IntegrityFailed as e:
        raise AudioFailed(str(e)) from None
    if probed is None:
        raise Skipped("ffprobe not found")
    streams = probed.get("streams") or []
    if not any(s.get("codec_type") == "video" for s in streams):
        raise Skipped("no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise Skipped("no audio stream")
    ext, muxer = _FORMATS.get(audio.get("codec_name"), _FALLBACK)
    return media.with_suffix(f".{ext}"), muxer


def target(media: Path, tools: Tools) -> Path:
    """The audio copy's path. Raises Skipped or AudioFailed, writes nothing."""
    return _plan(media, tools)[0]


def extract(media: Path, tools: Tools) -> Path:
    """Copy the first audio stream of `media` into a file beside it, without
    re-encoding. Returns its path. Raises Skipped or AudioFailed."""
    path, muxer = _plan(media, tools)
    if shutil.which("ffmpeg") is None:
        raise Skipped("ffmpeg not found")
    # A temporary name and a rename, so a crash never leaves a half file under
    # the final name. `-f` names the muxer the temporary suffix hides.
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        out = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(media),
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                muxer,
                str(tmp),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            raise AudioFailed(out.stderr.strip()[-500:] or f"ffmpeg exit code {out.returncode}")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


async def add(media: Path, tools: Tools) -> None:
    """The worker's step after a verified download. A failure logs a warning
    and leaves the download as it is."""
    try:
        path = await asyncio.to_thread(extract, media, tools)
    except Skipped as e:
        emit(log, logging.DEBUG, "audio.skipped", reason=str(e))
    except Exception as e:
        emit(log, logging.WARNING, "audio.failed", reason=f"{type(e).__name__}: {e}")
    else:
        size = human_bytes(path.stat().st_size)
        emit(log, logging.INFO, "audio.extracted", file=path.name, size=size)


def _recorded():
    """Whether a record job stored the video."""
    return (
        select(Job.id)
        .where(
            Job.video_id == Video.id,
            Job.kind == JobKind.RECORD,
            Job.state == JobState.SUCCEEDED,
        )
        .exists()
    )


def _media(archive_root: Path, video: Video) -> Path:
    # Files in a video directory are named `<video id>.<ext>` (layout.OUTTMPL).
    ext = (video.info or {}).get("ext")
    media = archive_root / video.dir_path / f"{video.id}.{ext}"
    if not ext or not media.is_file():
        raise AudioFailed("video file not found")
    return media


def _present(media: Path) -> Path | None:
    """The audio copy beside `media`, if one exists."""
    return next(
        (p for p in (media.with_suffix(s) for s in SUFFIXES) if p != media and p.is_file()),
        None,
    )


async def backfill(
    sessionmaker: async_sessionmaker,
    archive_root: Path,
    *,
    tools: Tools,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> dict[str, int]:
    """Write the missing audio copy of every stored video, one line each, and
    a summary line. A live recording is left to verify_recordings. Returns
    the counts."""
    recorded = _recorded()
    async with sessionmaker() as session:
        videos = (
            await session.scalars(
                select(Video)
                .where(Video.state == VideoState.COMPLETE, Video.dir_path.is_not(None), ~recorded)
                .order_by(Video.id)
            )
        ).all()
    verb = "would extract" if dry_run else "extracted"
    counts = {"extracted": 0, "skipped": 0, "failed": 0}
    for video in videos:
        directory = archive_root / video.dir_path
        try:
            media = _media(archive_root, video)
            present = _present(media)
            if present is not None:
                raise Skipped(f"has {present.name}")
            if dry_run:
                path = await asyncio.to_thread(target, media, tools)
            else:
                path = await asyncio.to_thread(extract, media, tools)
                async with sessionmaker() as session:
                    row = await session.get(Video, video.id)
                    # store() counts every file in the directory.
                    row.size_bytes = dir_size(directory)
                    await session.commit()
        except Skipped as e:
            counts["skipped"] += 1
            out(f"{video.id} skipped: {e}")
        except Exception as e:
            counts["failed"] += 1
            out(f"{video.id} failed: {e}")
        else:
            counts["extracted"] += 1
            out(f"{video.id} {verb} {path.relative_to(archive_root)}")
    out(f"{verb} {counts['extracted']}, skipped {counts['skipped']}, failed {counts['failed']}")
    return counts


async def verify_recordings(
    sessionmaker: async_sessionmaker,
    archive_root: Path,
    *,
    tools: Tools,
    dry_run: bool = False,
    out: Callable[[str], None] = print,
) -> dict[str, int]:
    """Verify every stored recording without `verified_at`, one at a time,
    write its missing audio copy, and set `verified_at`. One line each and a
    summary line. A failed recording stays unverified, so a later run tries
    it again. Returns the counts."""
    async with sessionmaker() as session:
        videos = (
            await session.scalars(
                select(Video)
                .where(
                    Video.state == VideoState.COMPLETE,
                    Video.dir_path.is_not(None),
                    Video.verified_at.is_(None),
                    _recorded(),
                )
                .order_by(Video.id)
            )
        ).all()
    verb = "would verify" if dry_run else "verified"
    counts = {"verified": 0, "failed": 0}
    for video in videos:
        try:
            media = _media(archive_root, video)
            if dry_run:
                done = str(media.relative_to(archive_root))
            else:
                checks = await asyncio.to_thread(verify_recording, media, video.info or {}, tools)
                if _present(media) is None:
                    try:
                        await asyncio.to_thread(extract, media, tools)
                    except Skipped:
                        pass
                async with sessionmaker() as session:
                    row = await session.get(Video, video.id)
                    row.verified_at = datetime.now(UTC)
                    row.verified_checks = checks
                    row.size_bytes = dir_size(media.parent)
                    await session.commit()
                done = ",".join(checks) or "no checks"
        except Exception as e:
            counts["failed"] += 1
            out(f"{video.id} failed: {e}")
        else:
            counts["verified"] += 1
            out(f"{video.id} {verb} {done}")
    out(f"{verb} {counts['verified']}, failed {counts['failed']}")
    return counts
