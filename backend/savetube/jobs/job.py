"""Running one job, archive or recording, from the `job` row to the tree."""

import asyncio
import logging
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
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
    _unavailable_streak,
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
    bare_unavailable,
    build_params,
    members_only,
    network_error,
    video_url,
    youtube_block,
)

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# Network interruptions in a row that added no bytes. A run that adds bytes
# resets the count, so a download that moves on is retried until it ends.
MAX_NETWORK_STALLS = 5
# Bare "Video unavailable" failures of different videos in a row, within the
# window, that read as a YouTube block rather than deleted videos.
UNAVAILABLE_STREAK = 3
UNAVAILABLE_STREAK_WINDOW = timedelta(hours=1)
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
    begun = await _begin(sessionmaker, job_id, settings, live=live)
    if begun is None:
        return
    r = _Run.after(begun, job_id, settings, live=live, sessionmaker=sessionmaker)
    with logs.context(job=job_id, video=r.video_id):
        r.log_start(begun)
        # Any failure past RUNNING must reach the row, or the job stays running and
        # no poll queues the video again.
        try:
            info, checks = await _download(r, begun, settings, runner, tools)
            await _finish(r, settings, info, checks, shorts=begun.shorts)
        except Disowned as e:
            r.log_disowned(str(e))
        except (asyncio.CancelledError, Paused) as e:
            # A recorder's retry starts from the first segment anyway.
            if live:
                raise
            await _record_stop(r, e)
            # A pause ends the queue job. The dispatcher queues the job again.
            if isinstance(e, Paused):
                return
            raise
        except Exception as e:
            if await _record_failure(r, e):
                raise


@dataclass
class _Begun:
    """What the run's first session read, once it marked the job running."""

    video_id: str
    ratelimit: int | None
    fmt: str
    shorts: bool
    members: bool
    profile: str | None
    pinned: str | None
    run_started: datetime
    attempt: int
    limit: int | None
    described: dict[str, Any]


async def _begin(
    sessionmaker: async_sessionmaker, job_id: int, settings: Settings, *, live: bool
) -> _Begun | None:
    """Mark the job running in one session. An archive job the limits keep
    from starting goes back to waiting, and this returns None."""
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
                await _hand_back(session, job_id, video_id, now)
                return None
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
        run_started = await _mark_running(session, job, video_id)
        attempt, limit = await _attempt(session, job, video_id, live=live)
        described = await _describe(session, video_id)
        await session.commit()
    return _Begun(
        video_id=video_id,
        ratelimit=ratelimit,
        fmt=fmt,
        shorts=shorts,
        members=members,
        profile=profile,
        pinned=pinned,
        run_started=run_started,
        attempt=attempt,
        limit=limit,
        described=described,
    )


async def _hand_back(session: AsyncSession, job_id: int, video_id: str, now: datetime) -> None:
    """Return an archive job the limits refuse to waiting, and log why."""
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


async def _mark_running(session: AsyncSession, job: Job, video_id: str) -> datetime:
    """Set the job and its video running. Returns the run's `started_at`."""
    job.deferred_until = None
    job.state = JobState.RUNNING
    job.attempts += 1
    # Marks the row as this run's (_owner_lost).
    run_started = job.started_at = datetime.now(UTC)
    await _set_video_state(session, video_id, VideoState.DOWNLOADING)
    return run_started


async def _attempt(
    session: AsyncSession, job: Job, video_id: str, *, live: bool
) -> tuple[int, int | None]:
    """This run's attempt number, and the attempts an archive job has."""
    if live:
        return job.attempts, None
    failed = await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.video_id == video_id,
            Job.kind == JobKind.ARCHIVE,
            Job.state == JobState.FAILED,
        )
    )
    return failed + 1, MAX_ARCHIVE_ATTEMPTS


def _present_label(workdir: Path) -> str | None:
    """The bytes in the working directory, for a log line."""
    return human_bytes(dir_size(workdir)) if workdir.exists() else None


@dataclass
class _Run:
    """One run past its `running` mark: its row, its directory, and its log lines."""

    job_id: int
    video_id: str
    live: bool
    sessionmaker: async_sessionmaker
    run_started: datetime
    described: dict[str, Any]
    workdir: Path
    attempt: int
    limit: int | None
    attempt_label: str
    started: float
    started_bytes: int
    streams: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def after(
        cls,
        begun: _Begun,
        job_id: int,
        settings: Settings,
        *,
        live: bool,
        sessionmaker: async_sessionmaker,
    ) -> _Run:
        video_id = begun.video_id
        workdir = settings.incoming_root / str(job_id) if live else incoming_dir(settings, video_id)
        attempt, limit = begun.attempt, begun.limit
        attempt_label = f"{attempt}/{limit}" if limit else str(attempt)
        started = time.monotonic()
        started_bytes = dir_size(workdir) if workdir.exists() else 0
        return cls(
            job_id=job_id,
            video_id=video_id,
            live=live,
            sessionmaker=sessionmaker,
            run_started=begun.run_started,
            described=begun.described,
            workdir=workdir,
            attempt=attempt,
            limit=limit,
            attempt_label=attempt_label,
            started=started,
            started_bytes=started_bytes,
        )

    def log_start(self, begun: _Begun) -> None:
        ratelimit = begun.ratelimit
        emit(
            log,
            logging.INFO,
            "job.start",
            job=self.job_id,
            video=self.video_id,
            **self.described,
            attempt=self.attempt_label,
            format_pref=begun.fmt,
            cookies=begun.profile or "none",
            ratelimit=f"{human_bytes(ratelimit)}/s" if ratelimit else None,
        )

    async def disowned(self, session: AsyncSession) -> bool:
        """Whether the row is no longer this run's, which then writes
        nothing and leaves the directory to the owner. Locks the row
        until the session commits."""
        if self.live:
            return False
        reason = await _owner_lost(session, self.job_id, self.run_started, for_update=True)
        if reason is not None:
            self.log_disowned(reason)
        return reason is not None

    def log_disowned(self, reason: str) -> None:
        emit(
            log,
            logging.WARNING,
            "job.disowned",
            job=self.job_id,
            video=self.video_id,
            **self.described,
            took=human_duration(time.monotonic() - self.started),
            reason=reason,
        )

    def interrupted(self, level: int, reason: str, present: str | None, **extra: Any) -> None:
        emit(
            log,
            level,
            "job.interrupted",
            job=self.job_id,
            video=self.video_id,
            **self.described,
            took=human_duration(time.monotonic() - self.started),
            present=present,
            **extra,
            reason=reason,
        )

    async def report(self, message: dict[str, Any]) -> None:
        if message.get("event") == "stream":
            self.streams.append(message)
            return
        async with self.sessionmaker() as session:
            if message.get("event") == "format":
                # The pin belongs to the run that owns the directory.
                if self.live or await _owner_lost(session, self.job_id, self.run_started) is None:
                    await _pin(session, self.video_id, message["format_id"])
            else:
                await session.execute(
                    update(Job)
                    .where(
                        Job.id == self.job_id,
                        Job.state == JobState.RUNNING,
                        Job.started_at == self.run_started,
                    )
                    .values(progress=message)
                )
            await session.commit()

    async def step(self, name: str) -> None:
        """Name the stage after the last byte on the job row: `merging`
        (from the child), `verifying`, or `audio`."""
        try:
            await self.report({"step": name})
        except Exception as e:
            # Display only. A failure here must not fail a stored download.
            emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")


def _params(
    workdir: Path,
    format: str,
    *,
    cookiefile: Path | None,
    live: bool,
    ratelimit: int | None,
    video_id: str,
) -> dict[str, Any]:
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


async def _download(
    r: _Run,
    begun: _Begun,
    settings: Settings,
    runner: Runner,
    tools: integrity.Tools | None,
) -> tuple[dict[str, Any], list[str] | None]:
    """Download into the working directory. An archive download is also
    verified and gets its audio copy, and returns the checks that passed.

    A members-only refusal of the general profile marks the video
    members-only, and the download runs once more with the channel's member
    profile, if one exists."""
    if r.live:
        prepare_incoming(settings.incoming_root, r.job_id)
    if begun.members and begun.profile is None:
        # A poll passed the video while a member profile existed.
        raise DownloadFailed(NO_MEMBER)
    download = partial(_download_as, r, begun, settings, runner, tools)
    try:
        info, checks = await download(begun.profile)
    except DownloadFailed as e:
        if begun.members or not members_only(str(e)):
            raise
        profile = await _mark_members(r.sessionmaker, r.video_id, settings)
        if profile is None:
            raise
        emit(log, logging.INFO, "job.member_retry", job=r.job_id, video=r.video_id, cookies=profile)
        info, checks = await download(profile)
        # The general profile's refusal proves the video members-only.
        info = {**info, "availability": "subscriber_only"}
    if not r.live:
        await r.step("audio")
        await audio.add(_media_path(info), tools or integrity.Tools())
    return info, checks


async def _mark_members(
    sessionmaker: async_sessionmaker, video_id: str, settings: Settings
) -> str | None:
    """Mark the video members-only. Returns its channel's member profile."""
    async with sessionmaker() as session:
        video = await session.get(Video, video_id)
        if video is None:
            return None
        video.type = VideoType.MEMBERS
        video.availability = "subscriber_only"
        profile = await cookies.member_profile(
            session, video.channel_id, cookies.profiles(settings)
        )
        await session.commit()
    return profile


async def _download_as(
    r: _Run,
    begun: _Begun,
    settings: Settings,
    runner: Runner,
    tools: integrity.Tools | None,
    profile: str | None,
) -> tuple[dict[str, Any], list[str] | None]:
    """One download with `profile`'s cookies, verified for an archive job."""
    # Outside the working directory, so the cookie jar never moves into the tree.
    with tempfile.TemporaryDirectory(prefix="savetube-") as private:
        cookiefile = cookies.cookiefile(settings, profile, Path(private))
        params = partial(
            _params,
            r.workdir,
            cookiefile=cookiefile,
            live=r.live,
            ratelimit=begun.ratelimit,
            video_id=r.video_id,
        )
        if r.live:
            return await runner(video_url(r.video_id), params(begun.fmt), r.report), None
        return await _unless_paused(
            r.sessionmaker,
            r.job_id,
            r.run_started,
            _archive(
                r.sessionmaker,
                r.video_id,
                r.workdir,
                runner=runner,
                params=params,
                fmt=begun.fmt,
                pinned=begun.pinned,
                report=r.report,
                step=r.step,
                streams=r.streams,
                tools=tools or integrity.Tools(),
            ),
        )


async def _finish(
    r: _Run,
    settings: Settings,
    info: dict[str, Any],
    checks: list[str] | None,
    *,
    shorts: bool,
) -> None:
    """Move the download into the tree, or into `ready/` on staging, and
    mark the job succeeded. A run that lost its row writes nothing."""
    async with r.sessionmaker() as session:
        if await r.disowned(session):
            return
        job = await session.get(Job, r.job_id)
        video = await store(
            session,
            job=job,
            info=info,
            incoming=r.workdir,
            archive_root=settings.archive_root,
            # A recording goes straight into the tree.
            ready_root=None if r.live else settings.ready_root,
            from_shorts_tab=shorts,
        )
        if checks is not None:
            video.resume_format = None
            video.verified_at = video.completed_at
            video.verified_checks = checks
        unblocked = 0 if r.live else await _unblock(session, datetime.now(UTC))
        await session.commit()
    if unblocked:
        emit(log, logging.INFO, "dispatch.unblocked", count=unblocked)
    emit(
        log,
        logging.INFO,
        "job.done",
        job=r.job_id,
        video=r.video_id,
        **r.described,
        size=human_bytes(video.size_bytes) if video.size_bytes is not None else None,
        took=human_duration(time.monotonic() - r.started),
        path=video.dir_path,
    )


async def _record_stop(r: _Run, e: asyncio.CancelledError | Paused) -> None:
    """Interrupt an archive job a shutdown or a pause stopped. A run that
    lost its row writes nothing."""
    reason = PAUSED_LOG if isinstance(e, Paused) else SHUTDOWN_LOG
    async with r.sessionmaker() as session:
        if await r.disowned(session):
            return
        job = await session.get(Job, r.job_id)
        await _interrupt(session, job, reason)
        await session.commit()
    r.interrupted(logging.INFO, reason, _present_label(r.workdir))


async def _record_failure(r: _Run, e: Exception) -> bool:
    """Write the failure to the job row and log it: a YouTube block, a streak
    of bare "Video unavailable" failures, or a network error interrupts an
    archive job, anything else fails it. Returns whether it wrote, False when
    the run lost its row."""
    message = str(e)
    if (
        not r.live
        and isinstance(e, DownloadFailed)
        and (
            youtube_block(message)
            # A bare "Video unavailable" alone may be a deleted video.
            or (
                bare_unavailable(message)
                and await _unavailable_streak(
                    r.sessionmaker, r.video_id, UNAVAILABLE_STREAK - 1, UNAVAILABLE_STREAK_WINDOW
                )
            )
        )
    ):
        if not await _blocked(r.sessionmaker, r.job_id, e, r.disowned):
            return False
        r.interrupted(logging.WARNING, BLOCKED_LOG, _present_label(r.workdir))
        setattr(e, _RECORDED, True)
        return True
    present = dir_size(r.workdir) if r.workdir.exists() else 0
    progressed = present > r.started_bytes
    if (
        not r.live
        and isinstance(e, DownloadFailed)
        and network_error(str(e))
        and (progressed or await _network_stalls(r.sessionmaker, r.video_id) < MAX_NETWORK_STALLS)
    ):
        return await _record_network(r, e, present, progressed=progressed)
    return await _record_failed(r, e)


async def _record_network(r: _Run, e: Exception, present: int, *, progressed: bool) -> bool:
    """Interrupt an archive job a network error stopped, keeping its parts.
    Returns False when the run lost its row."""
    prefix = NETWORK_LOG if progressed else NETWORK_STALL_LOG
    async with r.sessionmaker() as session:
        if await r.disowned(session):
            return False
        job = await session.get(Job, r.job_id)
        await _interrupt(session, job, f"{prefix}: {e}"[:LOG_TAIL_CHARS])
        await session.commit()
    r.interrupted(
        logging.WARNING,
        prefix,
        human_bytes(present),
        added=human_bytes(max(present - r.started_bytes, 0)),
    )
    setattr(e, _RECORDED, True)
    return True


async def _record_failed(r: _Run, e: Exception) -> bool:
    """Fail the job and delete its parts. Returns False when the run lost
    its row."""
    async with r.sessionmaker() as session:
        if await r.disowned(session):
            return False
        # A retry after a real failure starts over, so the parts would
        # otherwise stay on the dataset for good.
        shutil.rmtree(r.workdir, ignore_errors=True)
        job = await session.get(Job, r.job_id)
        job.state = JobState.FAILED
        job.finished_at = datetime.now(UTC)
        job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
        await _set_video_state(session, r.video_id, VideoState.FAILED)
        if not r.live:
            await _pin(session, r.video_id, None)
        await session.commit()
    final = r.live or (r.limit is not None and r.attempt >= r.limit)
    emit(
        log,
        logging.ERROR if final else logging.WARNING,
        "job.failed",
        job=r.job_id,
        video=r.video_id,
        **r.described,
        attempt=r.attempt_label,
        took=human_duration(time.monotonic() - r.started),
        reason=logs.cut(f"{type(e).__name__}: {e}"),
        # An expected failure is one line. Anything else needs its traceback.
        exc_info=not isinstance(e, DownloadFailed),
    )
    setattr(e, _RECORDED, True)
    return True
