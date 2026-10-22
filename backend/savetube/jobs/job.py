"""Running one job, archive or recording, from the `job` row to the tree."""

import asyncio
import logging
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import audio, cookies, integrity, logs
from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import Job, JobKind, JobState, Video, VideoState
from savetube.joblog import BLOCKED_LOG, NETWORK_LOG, NO_MEMBER, SHUTDOWN_LOG
from savetube.jobs.archive import _archive, _media_path
from savetube.jobs.guards import (
    NETWORK_STALL_LOG,
    Disowned,
    Paused,
    _blocked,
    _network_stalls,
    _unblock,
    _unless_paused,
)
from savetube.jobs.rows import (
    LOG_TAIL_CHARS,
    _describe,
    _interrupt,
    _owner_lost,
    _pin,
    _set_video_state,
    incoming_dir,
)
from savetube.jobs.runners import DownloadFailed, Runner
from savetube.layout import VideoType
from savetube.limits import (
    admit,
    blocked,
    capped,
    downloaded_today,
    load,
    window_opens,
)
from savetube.logs import emit, human_bytes, human_duration
from savetube.pipeline import from_shorts_tab, prepare_incoming, settings_channel, store
from savetube.poll import MAX_ARCHIVE_ATTEMPTS
from savetube.storage import dir_size
from savetube.ytdl import (
    DEFAULT_FORMAT,
    build_params,
    network_error,
    video_url,
    youtube_block,
)

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# Network interruptions in a row that added no bytes. A run that adds bytes
# resets the count, so a download that moves on is retried until it ends.
MAX_NETWORK_STALLS = 5
PAUSED_LOG = "paused by user"


_RECORDED = "savetube_recorded"


def recorded(e: BaseException) -> bool:
    """Whether `run` already wrote the failure to the job row and logged it."""
    return getattr(e, _RECORDED, False)


async def run(
    job_id: int,
    settings: Settings,
    *,
    live: bool,
    runner: Runner,
    sessionmaker: async_sessionmaker | None = None,
    tools: integrity.Tools | None = None,
) -> None:
    """Run one job. An archive job outside the download window, with no free
    slot, at the daily cap, or while paused, goes back to waiting for the
    dispatcher. A pause
    while it runs interrupts it. A recording ignores the limits."""
    sessionmaker = sessionmaker or default_sessionmaker()
    ratelimit = None
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None or job.video_id is None:
            raise LookupError(f"job {job_id} has no video")
        video_id = job.video_id
        if not live:
            now = datetime.now(UTC)
            limits = await admit(session, now)
            if limits is None:
                # A core UPDATE: the row loaded above may predate the
                # dispatcher's commit of queue_job_id.
                await session.execute(update(Job).where(Job.id == job_id).values(queue_job_id=None))
                await session.commit()
                current = await load(session)
                if current.paused:
                    reason = "paused"
                elif blocked(current, now) is not None:
                    reason = "blocked"
                elif capped(current, await downloaded_today(session, now), now):
                    reason = "daily_limit"
                elif window_opens(current, now):
                    reason = "window"
                else:
                    reason = "slots"
                emit(log, logging.INFO, "job.returned", job=job_id, video=video_id, reason=reason)
                return
            ratelimit = limits.rate_limit_bytes
        channel = await settings_channel(session, video_id)
        fmt = channel.format if channel is not None else DEFAULT_FORMAT
        shorts = await from_shorts_tab(session, video_id)
        video = await session.get(Video, video_id)
        pinned = video.resume_format if video is not None else None
        # A member profile serves its members-only videos only. Every other
        # download takes the general profile.
        members = video is not None and video.type is VideoType.MEMBERS
        if members:
            profile = await cookies.member_profile(
                session, video.channel_id, cookies.profiles(settings)
            )
        else:
            profile = await cookies.general(session)
        job.deferred_until = None
        job.state = JobState.RUNNING
        job.attempts += 1
        # Marks the row as this run's (_owner_lost).
        run_started = job.started_at = datetime.now(UTC)
        await _set_video_state(session, video_id, VideoState.DOWNLOADING)
        if live:
            attempt, limit = job.attempts, None
        else:
            failed = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.video_id == video_id,
                    Job.kind == JobKind.ARCHIVE,
                    Job.state == JobState.FAILED,
                )
            )
            attempt, limit = failed + 1, MAX_ARCHIVE_ATTEMPTS
        described = await _describe(session, video_id)
        await session.commit()

    workdir = settings.incoming_root / str(job_id) if live else incoming_dir(settings, video_id)
    attempt_label = f"{attempt}/{limit}" if limit else str(attempt)
    started = time.monotonic()
    started_bytes = dir_size(workdir) if workdir.exists() else 0
    with logs.context(job=job_id, video=video_id):
        emit(
            log,
            logging.INFO,
            "job.start",
            job=job_id,
            video=video_id,
            **described,
            attempt=attempt_label,
            format_pref=fmt,
            cookies=profile or "none",
            ratelimit=f"{human_bytes(ratelimit)}/s" if ratelimit else None,
        )

        streams: list[dict[str, Any]] = []

        async def disowned(session: AsyncSession) -> bool:
            """Whether the row is no longer this run's, which then writes
            nothing and leaves the directory to the owner. Locks the row
            until the session commits."""
            if live:
                return False
            reason = await _owner_lost(session, job_id, run_started, for_update=True)
            if reason is not None:
                log_disowned(reason)
            return reason is not None

        def log_disowned(reason: str) -> None:
            emit(
                log,
                logging.WARNING,
                "job.disowned",
                job=job_id,
                video=video_id,
                **described,
                took=human_duration(time.monotonic() - started),
                reason=reason,
            )

        def interrupted(level: int, reason: str, present: str | None, **extra: Any) -> None:
            emit(
                log,
                level,
                "job.interrupted",
                job=job_id,
                video=video_id,
                **described,
                took=human_duration(time.monotonic() - started),
                present=present,
                **extra,
                reason=reason,
            )

        async def report(message: dict[str, Any]) -> None:
            if message.get("event") == "stream":
                streams.append(message)
                return
            async with sessionmaker() as session:
                if message.get("event") == "format":
                    # The pin belongs to the run that owns the directory.
                    if live or await _owner_lost(session, job_id, run_started) is None:
                        await _pin(session, video_id, message["format_id"])
                else:
                    await session.execute(
                        update(Job)
                        .where(
                            Job.id == job_id,
                            Job.state == JobState.RUNNING,
                            Job.started_at == run_started,
                        )
                        .values(progress=message)
                    )
                await session.commit()

        async def step(name: str) -> None:
            """Name the stage after the last byte on the job row: `merging`
            (from the child), `verifying`, or `audio`."""
            try:
                await report({"step": name})
            except Exception as e:
                # Display only. A failure here must not fail a stored download.
                emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")

        # Any failure past RUNNING must reach the row, or the job stays running and
        # no poll queues the video again.
        try:
            if live:
                prepare_incoming(settings.incoming_root, job_id)
            # Outside the working directory, so the cookie jar never moves into the tree.
            with tempfile.TemporaryDirectory(prefix="savetube-") as private:
                if members and profile is None:
                    # A poll passed the video while a member profile existed.
                    raise DownloadFailed(NO_MEMBER)
                cookiefile = cookies.cookiefile(settings, profile, Path(private))

                def params(format: str) -> dict[str, Any]:
                    p = build_params(
                        workdir,
                        format=format,
                        cookiefile=cookiefile,
                        live=live,
                        ratelimit=ratelimit,
                    )
                    if not live:
                        # A `.part` file left by an interrupted run resumes.
                        p["continuedl"] = True
                        # yt-dlp rewrites every subtitle it finds, and the chat
                        # replay cannot resume. A finished one has its final name.
                        chat = f"{video_id}.live_chat.json"
                        if (workdir / chat).is_file() or (workdir / ".tmp" / chat).is_file():
                            p["subtitleslangs"] = [*p["subtitleslangs"], "-live_chat"]
                    return p

                checks = None
                if live:
                    info = await runner(video_url(video_id), params(fmt), report)
                else:
                    info, checks = await _unless_paused(
                        sessionmaker,
                        job_id,
                        run_started,
                        _archive(
                            sessionmaker,
                            video_id,
                            workdir,
                            runner=runner,
                            params=params,
                            fmt=fmt,
                            pinned=pinned,
                            report=report,
                            step=step,
                            streams=streams,
                            tools=tools or integrity.Tools(),
                        ),
                    )
                    await step("audio")
                    await audio.add(_media_path(info), tools or integrity.Tools())

            async with sessionmaker() as session:
                if await disowned(session):
                    return
                job = await session.get(Job, job_id)
                video = await store(
                    session,
                    job=job,
                    info=info,
                    incoming=workdir,
                    archive_root=settings.archive_root,
                    # A recording goes straight into the tree.
                    ready_root=None if live else settings.ready_root,
                    from_shorts_tab=shorts,
                )
                if checks is not None:
                    video.resume_format = None
                    video.verified_at = video.completed_at
                    video.verified_checks = checks
                unblocked = 0 if live else await _unblock(session, datetime.now(UTC))
                await session.commit()
            if unblocked:
                emit(log, logging.INFO, "dispatch.unblocked", count=unblocked)
            emit(
                log,
                logging.INFO,
                "job.done",
                job=job_id,
                video=video_id,
                **described,
                size=human_bytes(video.size_bytes) if video.size_bytes is not None else None,
                took=human_duration(time.monotonic() - started),
                path=video.dir_path,
            )
        except Disowned as e:
            log_disowned(str(e))
        except (asyncio.CancelledError, Paused) as e:
            # A recorder's retry starts from the first segment anyway.
            if live:
                raise
            reason = PAUSED_LOG if isinstance(e, Paused) else SHUTDOWN_LOG
            async with sessionmaker() as session:
                if await disowned(session):
                    if isinstance(e, Paused):
                        return
                    raise
                job = await session.get(Job, job_id)
                await _interrupt(session, job, reason)
                await session.commit()
            interrupted(
                logging.INFO,
                reason,
                human_bytes(dir_size(workdir)) if workdir.exists() else None,
            )
            # A pause ends the queue job. The dispatcher queues the job again.
            if isinstance(e, Paused):
                return
            raise
        except Exception as e:
            if not live and isinstance(e, DownloadFailed) and youtube_block(str(e)):
                if not await _blocked(sessionmaker, job_id, e, disowned):
                    return
                interrupted(
                    logging.WARNING,
                    BLOCKED_LOG,
                    human_bytes(dir_size(workdir)) if workdir.exists() else None,
                )
                setattr(e, _RECORDED, True)
                raise
            present = dir_size(workdir) if workdir.exists() else 0
            progressed = present > started_bytes
            if (
                not live
                and isinstance(e, DownloadFailed)
                and network_error(str(e))
                and (
                    progressed or await _network_stalls(sessionmaker, video_id) < MAX_NETWORK_STALLS
                )
            ):
                prefix = NETWORK_LOG if progressed else NETWORK_STALL_LOG
                async with sessionmaker() as session:
                    if await disowned(session):
                        return
                    job = await session.get(Job, job_id)
                    await _interrupt(session, job, f"{prefix}: {e}"[:LOG_TAIL_CHARS])
                    await session.commit()
                interrupted(
                    logging.WARNING,
                    prefix,
                    human_bytes(present),
                    added=human_bytes(max(present - started_bytes, 0)),
                )
                setattr(e, _RECORDED, True)
                raise
            async with sessionmaker() as session:
                if await disowned(session):
                    return
                # A retry after a real failure starts over, so the parts would
                # otherwise stay on the dataset for good.
                shutil.rmtree(workdir, ignore_errors=True)
                job = await session.get(Job, job_id)
                job.state = JobState.FAILED
                job.finished_at = datetime.now(UTC)
                job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
                await _set_video_state(session, video_id, VideoState.FAILED)
                if not live:
                    await _pin(session, video_id, None)
                await session.commit()
            final = live or (limit is not None and attempt >= limit)
            emit(
                log,
                logging.ERROR if final else logging.WARNING,
                "job.failed",
                job=job_id,
                video=video_id,
                **described,
                attempt=attempt_label,
                took=human_duration(time.monotonic() - started),
                reason=logs.cut(f"{type(e).__name__}: {e}"),
                # An expected failure is one line. Anything else needs its traceback.
                exc_info=not isinstance(e, DownloadFailed),
            )
            setattr(e, _RECORDED, True)
            raise
