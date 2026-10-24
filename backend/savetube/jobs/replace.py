"""A replace job: one stream of a stored live swapped for a better one
(docs/plan/live.md, Format watch).

The job downloads only the streams it names and remuxes them with the kept
stream of the stored file through `ffmpeg -c copy`. The result passes the
download's integrity checks, then takes the stored file's place in the tree.
The first stored file moves aside until the watch ends."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube import audio, cookies, formats, integrity, logs
from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import FormatWatch, Job, JobState, Video
from savetube.joblog import BLOCKED_LOG, SHUTDOWN_LOG
from savetube.jobs.archive import _media_path
from savetube.jobs.guards import Disowned, Paused, _blocked, _unless_paused
from savetube.jobs.job import PAUSED_LOG, _hand_back
from savetube.jobs.rows import LOG_TAIL_CHARS, _describe, _interrupt, _owner_lost
from savetube.jobs.runners import DownloadFailed, Runner
from savetube.layout import VideoType
from savetube.limits import admit
from savetube.logs import emit, human_bytes, human_duration
from savetube.storage import dir_size
from savetube.watch import media_path, originals_dir, probe_duration
from savetube.ytdl import build_params, video_url, youtube_block

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")


class ReplaceFailed(Exception):
    pass


@dataclass
class _Begun:
    video_id: str
    format_id: str
    run_started: datetime
    ratelimit: int | None
    profile: str | None
    vod_duration: float | None
    described: dict[str, Any]


async def run(
    job_id: int,
    settings: Settings,
    *,
    runner: Runner,
    sessionmaker: async_sessionmaker | None = None,
    tools: integrity.Tools | None = None,
) -> None:
    """Run one replace job. It waits for the archive download limits as an
    archive job does."""
    sessionmaker = sessionmaker or default_sessionmaker()
    tools = tools or integrity.Tools()
    begun = await _begin(sessionmaker, job_id, settings)
    if begun is None:
        return
    # Numbered by job, so the incoming cleanup keeps them while the job is active.
    workdir = settings.download_root / str(job_id)
    outdir = settings.incoming_root / str(job_id)
    started = time.monotonic()
    with logs.context(job=job_id, video=begun.video_id):
        emit(
            log,
            logging.INFO,
            "replace.start",
            job=job_id,
            video=begun.video_id,
            **begun.described,
            format=begun.format_id,
            cookies=begun.profile or "none",
        )
        # A run that lost its row leaves the directories to the run that took it.
        owned = True
        try:
            path = await _replace(
                sessionmaker, settings, job_id, begun, workdir, outdir, runner, tools
            )
            emit(
                log,
                logging.INFO,
                "replace.done",
                job=job_id,
                video=begun.video_id,
                format=begun.format_id,
                took=human_duration(time.monotonic() - started),
                file=path.name,
            )
        except Disowned as e:
            owned = False
            emit(
                log,
                logging.WARNING,
                "job.disowned",
                job=job_id,
                video=begun.video_id,
                reason=str(e),
            )
        except (asyncio.CancelledError, Paused) as e:
            reason = PAUSED_LOG if isinstance(e, Paused) else SHUTDOWN_LOG
            owned = await _stop(sessionmaker, job_id, begun, reason)
            if isinstance(e, asyncio.CancelledError):
                raise
        except DownloadFailed as e:
            if youtube_block(str(e)):

                async def disowned(session) -> bool:
                    return (
                        await _owner_lost(session, job_id, begun.run_started, for_update=True)
                        is not None
                    )

                owned = await _blocked(sessionmaker, job_id, e, disowned)
                emit(
                    log,
                    logging.WARNING,
                    "job.interrupted",
                    job=job_id,
                    video=begun.video_id,
                    reason=BLOCKED_LOG,
                )
            else:
                owned = await _fail(sessionmaker, job_id, begun, e)
        except Exception as e:
            owned = await _fail(sessionmaker, job_id, begun, e)
        finally:
            if owned:
                shutil.rmtree(workdir, ignore_errors=True)
                shutil.rmtree(outdir, ignore_errors=True)


async def _begin(
    sessionmaker: async_sessionmaker, job_id: int, settings: Settings
) -> _Begun | None:
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None or job.video_id is None or not job.format_id:
            raise LookupError(f"job {job_id} has no video or format")
        now = datetime.now(UTC)
        limits = await admit(session, now)
        if limits is None:
            await _hand_back(session, job_id, job.video_id, now)
            return None
        video = await session.get(Video, job.video_id)
        watch = await session.get(FormatWatch, job.video_id)
        if video.type is VideoType.MEMBERS:
            profile = await cookies.member_profile(
                session, video.channel_id, cookies.profiles(settings)
            )
        else:
            profile = await cookies.general(session)
        job.deferred_until = None
        job.state = JobState.RUNNING
        job.attempts += 1
        job.started_at = now
        described = await _describe(session, job.video_id)
        await session.commit()
    return _Begun(
        video_id=job.video_id,
        format_id=job.format_id,
        run_started=now,
        ratelimit=limits.rate_limit_bytes,
        profile=profile,
        vod_duration=watch.vod_duration if watch is not None else None,
        described=described,
    )


async def _replace(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    job_id: int,
    begun: _Begun,
    workdir: Path,
    outdir: Path,
    runner: Runner,
    tools: integrity.Tools,
) -> Path:
    for d in (workdir, outdir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    streams: list[dict[str, Any]] = []

    async def report(message: dict[str, Any]) -> None:
        if message.get("event") == "stream":
            streams.append(message)
            return
        if message.get("event"):
            return
        async with sessionmaker() as session:
            await session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.state == JobState.RUNNING,
                    Job.started_at == begun.run_started,
                )
                .values(progress=message)
            )
            await session.commit()

    with tempfile.TemporaryDirectory(prefix="savetube-") as private:
        params = build_params(
            workdir,
            format=begun.format_id,
            cookiefile=cookies.cookiefile(settings, begun.profile, Path(private)),
            ratelimit=begun.ratelimit,
        )
        # Only the media: the stored directory keeps its own sidecar files.
        params.update(writeinfojson=False, writethumbnail=False, writesubtitles=False)
        info = await _unless_paused(
            sessionmaker,
            job_id,
            begun.run_started,
            runner(video_url(begun.video_id), params, report),
        )
    downloaded = _media_path(info) or workdir / "missing"
    await _step(sessionmaker, job_id, begun, "verifying")
    try:
        await asyncio.to_thread(
            integrity.verify, downloaded, info, list(streams), resumed=False, tools=tools
        )
    except integrity.IntegrityFailed as e:
        raise ReplaceFailed(f"integrity check failed: {e}") from None

    async with sessionmaker() as session:
        video = await session.get(Video, begun.video_id)
        watch = await session.get(FormatWatch, begun.video_id)
        stored = media_path(settings.archive_root, video)
        original_path = watch.original_path
        old_info = video.info or {}
    if stored is None or not stored.is_file():
        raise ReplaceFailed("stored file not found")
    new = formats.stored_streams(info)
    # The output is on the archive's filesystem, so it renames into the tree.
    out = outdir / f"{begun.video_id}.mkv"
    await _step(sessionmaker, job_id, begun, "merging")
    await asyncio.to_thread(
        _remux, stored, downloaded, out, video="video" in new, audio="audio" in new
    )
    await _step(sessionmaker, job_id, begun, "verifying")
    try:
        # The stream copy reads every packet of the result.
        checks = await asyncio.to_thread(
            integrity.verify,
            out,
            {"vcodec": "kept", "acodec": "kept"},
            [],
            resumed=True,
            tools=tools,
        )
    except integrity.IntegrityFailed as e:
        raise ReplaceFailed(f"integrity check failed: {e}") from None

    # Before the row lock below: this writes the same row from its own session.
    await _step(sessionmaker, job_id, begun, "audio")
    async with sessionmaker() as session:
        if await _owner_lost(session, job_id, begun.run_started, for_update=True) is not None:
            raise Disowned("the job row changed before the swap")
        final = stored.with_name(out.name)
        aside = await asyncio.to_thread(
            _swap,
            settings.archive_root,
            begun.video_id,
            stored,
            out,
            keep_original=original_path is None,
        )
        await audio.add(final, tools)
        merged = _merged_info(old_info, new, final)
        _write_info_json(final.parent / f"{begun.video_id}.info.json", merged)
        video = await session.get(Video, begun.video_id)
        watch = await session.get(FormatWatch, begun.video_id)
        video.info = merged
        video.format_id = merged.get("format_id")
        video.size_bytes = dir_size(final.parent)
        video.verified_at = datetime.now(UTC)
        video.verified_checks = checks
        if aside is not None:
            watch.original_path = aside.relative_to(settings.archive_root).as_posix()
        try:
            watch.stored_duration = await asyncio.to_thread(probe_duration, final, tools)
        except integrity.IntegrityFailed:
            watch.stored_duration = None
        job = await session.get(Job, job_id)
        job.state = JobState.SUCCEEDED
        job.finished_at = video.verified_at
        job.progress = None
        await session.commit()
    emit(
        log,
        logging.INFO,
        "replace.swapped",
        video=begun.video_id,
        size=human_bytes(video.size_bytes),
        original=watch.original_path or "none",
    )
    return final


async def _step(sessionmaker: async_sessionmaker, job_id: int, begun: _Begun, name: str) -> None:
    try:
        async with sessionmaker() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.started_at == begun.run_started)
                .values(progress={"step": name})
            )
            await session.commit()
    except Exception as e:
        # Display only. A failure here must not fail the replace.
        emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")


def _remux(stored: Path, downloaded: Path, out: Path, *, video: bool, audio: bool) -> None:
    """Write `out` from the downloaded streams and the stored file's other
    stream, without re-encoding."""
    if shutil.which("ffmpeg") is None:
        raise ReplaceFailed("ffmpeg not found")
    maps = ["-map", "1:v:0" if video else "0:v:0", "-map", "1:a:0" if audio else "0:a:0"]
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(stored), "-i", str(downloaded),
         *maps, "-c", "copy", "-f", "matroska", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise ReplaceFailed(result.stderr.strip()[-500:] or f"ffmpeg exit code {result.returncode}")


def _swap(
    archive_root: Path, video_id: str, stored: Path, out: Path, *, keep_original: bool
) -> Path | None:
    """Put `out` in the stored file's place. With `keep_original`, the stored
    file first gets a hard link in the originals directory, which this
    returns. Otherwise the stored file came from an earlier replace and goes.
    The link comes before the rename, so no step leaves the tree without the
    file. An old audio copy goes too: the caller writes a new one."""
    aside = None
    if keep_original:
        aside = originals_dir(archive_root, video_id) / stored.name
        aside.parent.mkdir(parents=True, exist_ok=True)
        # A link left by a run cut off before its commit is the original.
        if not aside.exists():
            os.link(stored, aside)
    final = stored.with_name(out.name)
    os.replace(out, final)
    if stored != final:
        stored.unlink(missing_ok=True)
    for suffix in audio.SUFFIXES:
        old = final.with_suffix(suffix)
        if old != final:
            old.unlink(missing_ok=True)
    return aside


def _merged_info(
    old: dict[str, Any], new: dict[str, dict[str, Any]], final: Path
) -> dict[str, Any]:
    """The stored info dict with the replaced streams' format fields."""
    streams = {**formats.stored_streams(old), **{k: formats.trim(v) for k, v in new.items()}}
    ordered = [streams[k] for k in ("video", "audio") if k in streams]
    merged = {**old, "requested_formats": ordered, "ext": final.suffix.lstrip(".")}
    merged["format_id"] = "+".join(str(f.get("format_id")) for f in ordered)
    merged["format"] = " + ".join(str(f.get("format") or f.get("format_id")) for f in ordered)
    if "video" in streams:
        v = streams["video"]
        for key in ("vcodec", "width", "height", "fps", "resolution", "dynamic_range", "vbr"):
            merged[key] = v.get(key)
    if "audio" in streams:
        a = streams["audio"]
        for key in ("acodec", "abr", "asr", "audio_channels"):
            merged[key] = a.get(key)
    merged["tbr"] = sum(f.get("tbr") or 0 for f in ordered) or None
    return merged


def _write_info_json(path: Path, info: dict[str, Any]) -> None:
    """The `.info.json` beside the video is the source of truth for a rescan."""
    if not path.is_file():
        return
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False))
    os.replace(tmp, path)


async def _stop(sessionmaker: async_sessionmaker, job_id: int, begun: _Begun, reason: str) -> bool:
    """Put a replace a shutdown or a pause stopped back to waiting. Returns
    False when the run lost its row."""
    async with sessionmaker() as session:
        if await _owner_lost(session, job_id, begun.run_started, for_update=True) is not None:
            return False
        await _interrupt(session, await session.get(Job, job_id), reason)
        await session.commit()
    emit(log, logging.INFO, "job.interrupted", job=job_id, video=begun.video_id, reason=reason)
    return True


async def _fail(sessionmaker: async_sessionmaker, job_id: int, begun: _Begun, e: Exception) -> bool:
    """Fail the job. The stored file is untouched, and the next probe may
    queue another replace. Returns False when the run lost its row."""
    async with sessionmaker() as session:
        if await _owner_lost(session, job_id, begun.run_started, for_update=True) is not None:
            return False
        job = await session.get(Job, job_id)
        job.state = JobState.FAILED
        job.finished_at = datetime.now(UTC)
        job.progress = None
        job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
        await session.commit()
    emit(
        log,
        logging.ERROR,
        "job.failed",
        job=job_id,
        video=begun.video_id,
        **begun.described,
        reason=logs.cut(f"{type(e).__name__}: {e}"),
        exc_info=not isinstance(e, (DownloadFailed, ReplaceFailed)),
    )
    return True
