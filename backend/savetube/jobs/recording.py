"""Recording one live in its recorder pod, without the database.

The scheduler reads what a recording needs when it creates the recorder Job
(savetube.recorder_args.Recording), and applies the outcome file the recorder leaves
(savetube.recorders). A database outage then costs no live: the recorder's one
database write is its progress, which is for display only.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from savetube import audio, cookies, formats, integrity, watch
from savetube.config import Settings
from savetube.db.models import Job, JobState
from savetube.joblog import NO_MEMBER
from savetube.jobs.archive import _media_path
from savetube.jobs.runners import DownloadFailed, Report, Runner
from savetube.logs import cut, emit, human_bytes, human_duration
from savetube.pipeline import prepare_incoming
from savetube.recorder_args import Recording
from savetube.ytdl import NotStarted, build_params, members_only, video_url

log = logging.getLogger(__package__)

# How long past its scheduled start a recording waits for a stream still
# upcoming, such as a cancelled waiting room. The span after which live
# detection probes a refused id again (savetube.live.RETRY_UNREADABLE).
NOT_STARTED_LIMIT = timedelta(hours=24)
NOT_STARTED_LOG = "the stream did not start within 24 hours of its scheduled start"

Outcome = Literal["recorded", "not_started", "failed"]


def workdir(incoming_root: Path, job_id: int) -> Path:
    return incoming_root / str(job_id)


def outcome_path(incoming_root: Path, job_id: int) -> Path:
    """Beside the working directory, so the move into the tree leaves it."""
    return incoming_root / f"{job_id}.json"


def read_outcome(incoming_root: Path, job_id: int) -> dict[str, Any] | None:
    path = outcome_path(incoming_root, job_id)
    return json.loads(path.read_text()) if path.exists() else None


def _write_outcome(incoming_root: Path, job_id: int, outcome: Outcome, **fields: Any) -> None:
    path = outcome_path(incoming_root, job_id)
    # A rename, so the scheduler never reads a half-written file.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"outcome": outcome, **fields}))
    os.replace(tmp, path)


def progress_writer(sessionmaker: async_sessionmaker, job_id: int) -> Report:
    """The recorder's progress, written to its running job row."""

    async def write(message: dict[str, Any]) -> None:
        async with sessionmaker() as session:
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.state == JobState.RUNNING)
                .values(progress=message)
            )
            await session.commit()

    return write


async def record(
    rec: Recording,
    settings: Settings,
    *,
    runner: Runner,
    report: Report,
    tools: integrity.Tools,
) -> None:
    """Record the live into its working directory and write the outcome. A
    stream that never started writes `not_started` and returns. Any other
    failure deletes the parts, writes `failed` and raises, so the Job
    retries from the first segment."""
    root = settings.incoming_root
    work = workdir(root, rec.job_id)
    outcome_path(root, rec.job_id).unlink(missing_ok=True)
    prepare_incoming(root, rec.job_id)
    started = time.monotonic()
    emit(
        log,
        logging.INFO,
        "record.start",
        job=rec.job_id,
        video=rec.video_id,
        format_pref=rec.format,
        cookies=rec.profile or "none",
        wait_until=rec.wait_until.isoformat() if rec.wait_until else None,
    )
    streams: list[dict[str, Any]] = []
    sink = _sink(streams, report)
    try:
        info = await _download(rec, settings, work, runner, sink)
        media = _media_path(info) or work / "missing"
        await _step(sink, "verifying")
        checks = await _verify(media, info, tools)
        if not info.get("duration"):
            info = await _with_duration(info, media, tools)
        await _step(sink, "audio")
        await audio.add(media, tools)
    except NotStarted:
        shutil.rmtree(work, ignore_errors=True)
        _write_outcome(root, rec.job_id, "not_started")
        emit(log, logging.WARNING, "record.not_started", job=rec.job_id, video=rec.video_id)
        return
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        error = str(e) or type(e).__name__
        _write_outcome(root, rec.job_id, "failed", error=error)
        emit(
            log,
            logging.ERROR,
            "record.failed",
            job=rec.job_id,
            video=rec.video_id,
            took=human_duration(time.monotonic() - started),
            reason=cut(f"{type(e).__name__}: {e}"),
            # An expected failure is one line. Anything else needs its traceback.
            exc_info=not isinstance(e, DownloadFailed),
        )
        raise
    # The recorder gets no stream sizes from YouTube. The video page shows the measured ones.
    info = formats.with_sizes(info, streams)
    _write_outcome(root, rec.job_id, "recorded", info=info, checks=checks)
    emit(
        log,
        logging.INFO,
        "record.done",
        job=rec.job_id,
        video=rec.video_id,
        size=human_bytes(sum(f.stat().st_size for f in work.rglob("*") if f.is_file())),
        took=human_duration(time.monotonic() - started),
    )


def _sink(streams: list[dict[str, Any]], report: Report) -> Report:
    """Keep each stream event for the stored info and pass the rest to `report`."""

    async def send(message: dict[str, Any]) -> None:
        if message.get("event") == "stream":
            streams.append(message)
            return
        await report(message)

    return send


async def _step(report: Report, name: str) -> None:
    """Name the stage after the last byte: `verifying` or `audio`."""
    try:
        await report({"step": name})
    except Exception as e:
        # Display only. A failure here must not fail the recording.
        emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")


async def _download(
    rec: Recording, settings: Settings, work: Path, runner: Runner, report: Report
) -> dict[str, Any]:
    """The download with `rec.profile`, and once more with the member profile
    when YouTube calls a video not known as members-only so."""
    if rec.members and rec.profile is None:
        # Detection queued the video while a member profile existed.
        raise DownloadFailed(NO_MEMBER)
    download = _download_as(rec, settings, work, runner, report)
    try:
        return await download(rec.profile)
    except DownloadFailed as e:
        if rec.members or not members_only(str(e)) or rec.member_profile is None:
            raise
    emit(
        log,
        logging.INFO,
        "job.member_retry",
        job=rec.job_id,
        video=rec.video_id,
        cookies=rec.member_profile,
    )
    info = await download(rec.member_profile)
    # Members-only, as savetube.jobs.job._download marks an archive download.
    return {**info, "availability": "subscriber_only"}


def _download_as(
    rec: Recording, settings: Settings, work: Path, runner: Runner, report: Report
) -> Callable[[str | None], Awaitable[dict[str, Any]]]:
    async def download(profile: str | None) -> dict[str, Any]:
        # The cookie jar stays outside the tree, as in savetube.jobs.job._download_as.
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            params = build_params(
                work,
                format=rec.format,
                cookiefile=cookies.cookiefile(settings, profile, Path(private)),
                live=True,
                wait_until=rec.wait_until.timestamp() if rec.wait_until else None,
            )
            return await runner(video_url(rec.video_id), params, report)

    return download


async def _verify(media: Path, info: dict[str, Any], tools: integrity.Tools) -> list[str] | None:
    """The recording's checks, None when one fails. A failure keeps the
    recording: it is the only copy of the live, so it is stored unverified."""
    try:
        checks = await asyncio.to_thread(integrity.verify_recording, media, info, tools)
    except integrity.IntegrityFailed as e:
        emit(log, logging.WARNING, "record.integrity_failed", check=e.check, **e.numbers)
        return None
    except Exception as e:
        # Any other error would fail the recording, which deletes it.
        emit(
            log,
            logging.WARNING,
            "record.integrity_failed",
            check="error",
            reason=f"{type(e).__name__}: {e}",
        )
        return None
    emit(log, logging.INFO, "download.verified", checks=",".join(checks) or "none")
    return checks


async def _with_duration(
    info: dict[str, Any], media: Path, tools: integrity.Tools
) -> dict[str, Any]:
    """`info` with the file's duration: yt-dlp gives a live none, and the
    Videos page shows it. A failed probe stores the live without one, as
    `_verify` keeps a recording that fails its checks."""
    try:
        duration = await asyncio.to_thread(watch.probe_duration, media, tools)
    except Exception as e:
        emit(log, logging.WARNING, "record.duration_failed", reason=f"{type(e).__name__}: {e}")
        return info
    return {**info, "duration": round(duration)} if duration is not None else info
