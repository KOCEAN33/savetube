"""The single-replica loop: source polls, archive dispatch, live detection, recorder
Jobs, the format watch, storage samples, and the internal HTTP API.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from procrastinate.jobs import Status
from sqlalchemy import func, select

from savetube import cookies, encode, k8s, scheduler_api
from savetube.cleanup import clean_database
from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import StorageSample
from savetube.integrity import Tools
from savetube.jobs import Dispatched, clean_incoming, dispatch, reclaim_stalled
from savetube.live import check_logged, watched_channels
from savetube.logs import emit, human_bytes
from savetube.poll import due_sources, poll_source
from savetube.queue import ARCHIVE_QUEUE, app, enqueue
from savetube.recorders import start_recorders
from savetube.staging import move_staged
from savetube.storage import disk_usage
from savetube.tz import TIMEZONE
from savetube.watch import watch_formats
from savetube.ytdl import (
    fetch_feed,
    list_entries,
    list_streams,
    probe_formats,
    probe_live,
    probe_member,
    probe_video,
    read_account,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 60
# With max_concurrent 1 a slot sits idle up to this long after each download,
# so it is shorter than the tick. One dispatch is a few small queries.
DISPATCH_SECONDS = 10
SAMPLE_EVERY = timedelta(hours=1)
SUMMARY_SECONDS = 600
# Partial directories and finished rows age in days, so an hourly look is enough.
CLEANUP_SECONDS = 3600
# A stream found live waits up to this long for its recorder.
RECORDER_SECONDS = 15


async def sample_storage(settings: Settings) -> None:
    async with default_sessionmaker()() as session:
        last = await session.scalar(select(func.max(StorageSample.sampled_at)))
        now = datetime.now(UTC)
        if last is not None and now - last < SAMPLE_EVERY:
            return
        total, free = disk_usage(settings.archive_root)
        emit(log, logging.DEBUG, "storage.sample", free=human_bytes(free), total=human_bytes(total))
        session.add(StorageSample(sampled_at=now, total_bytes=total, free_bytes=free))
        await session.commit()


async def poll_sources(settings: Settings, requests: scheduler_api.Requests) -> None:
    """Poll the user's requested sources, then the due ones. A request made
    while the round runs goes ahead of the due sources left. A source polls at
    most once per round: a request for one this round polled, perhaps minutes
    ago, waits for the next round, which it starts at once."""

    async def cancel(queue_job_id: int) -> bool:
        return await app.job_manager.cancel_job_by_id_async(queue_job_id)

    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        due = await due_sources(session, datetime.now(UTC))
    polled: set[int] = set()
    later: list[int] = []
    try:
        # One at a time: every listing counts against YouTube's request limits.
        while True:
            source_id = requests.take_poll()
            requested = source_id is not None
            if not requested:
                source_id = next((s for s in due if s not in polled), None)
                if source_id is None:
                    return
            if source_id in polled:
                later.append(source_id)
                continue
            polled.add(source_id)
            if requested:
                emit(log, logging.INFO, "poll.requested", source=source_id)
            await poll_source(
                sessionmaker,
                source_id,
                settings,
                lister=list_entries,
                cancel=cancel,
                prober=probe_member,
            )
    finally:
        for source_id in later:
            requests.ask_poll(source_id)


class DispatchLog:
    """Logs a dispatch that changes something: jobs sent, a new reason to wait,
    and a summary every 10 minutes while a job waits or runs."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._reason: str | None = None
        self._summary_at: float | None = None

    def __call__(self, d: Dispatched) -> None:
        if d.requeued:
            emit(
                log,
                logging.WARNING,
                "dispatch.requeued",
                jobs=",".join(map(str, d.requeued)),
                reason="queue job ended before the worker marked the row",
            )
        if d.started:
            emit(
                log,
                logging.INFO,
                "dispatch.sent",
                jobs=",".join(map(str, d.started)),
                running=d.running,
                max=d.max_concurrent,
                waiting=d.waiting,
            )
        reason = None
        if d.waiting:
            if d.paused:
                reason = "paused"
            elif d.blocked_until is not None:
                reason = "blocked"
            elif d.capped_until is not None:
                reason = "daily_limit"
            else:
                reason = "window" if d.opens is not None else "slots"
        if reason != self._reason:
            self._reason = reason
            if reason in ("window", "blocked"):
                emit(
                    log,
                    logging.INFO,
                    "dispatch.waiting",
                    reason=reason,
                    opens=d.opens.astimezone(TIMEZONE).isoformat(timespec="minutes"),
                    waiting=d.waiting,
                )
            elif reason == "daily_limit":
                emit(
                    log,
                    logging.INFO,
                    "dispatch.waiting",
                    reason=reason,
                    opens=d.opens.astimezone(TIMEZONE).isoformat(timespec="minutes"),
                    today=f"{d.today}/{d.daily_limit}",
                    waiting=d.waiting,
                )
            elif reason == "paused":
                emit(log, logging.INFO, "dispatch.waiting", reason=reason, waiting=d.waiting)
            elif reason == "slots":
                emit(
                    log,
                    logging.INFO,
                    "dispatch.waiting",
                    reason=reason,
                    running=d.running + len(d.started),
                    max=d.max_concurrent,
                    waiting=d.waiting,
                )
        now = self._clock()
        if not (d.waiting or d.running or d.started):
            self._summary_at = None
        elif self._summary_at is None or now - self._summary_at >= SUMMARY_SECONDS:
            self._summary_at = now
            emit(
                log,
                logging.INFO,
                "queue.summary",
                waiting=d.waiting,
                running=d.running,
                max=d.max_concurrent,
                window=d.window or "none",
                open="no" if d.opens is not None else "yes",
                ratelimit=f"{human_bytes(d.rate_limit)}/s" if d.rate_limit else "none",
                blocked=d.blocked_until.astimezone(TIMEZONE).isoformat(timespec="minutes")
                if d.blocked_until
                else "none",
                paused="yes" if d.paused else "no",
            )


async def dispatch_archives(report: DispatchLog) -> None:
    async with default_sessionmaker()() as session:
        report(await dispatch(session, datetime.now(UTC), enqueue))


# A live worker misses heartbeats during a short database outage, such as a
# Postgres failover, so 30 s, procrastinate's default, reclaims its jobs. A
# worker that the others pruned leaves its jobs with no worker, and those read
# as stalled at any threshold. The run's ownership check covers both.
STALLED_HEARTBEAT_SECONDS = 300


async def reclaim(settings: Settings) -> None:
    async def stalled() -> list[tuple[int, int]]:
        found = await app.job_manager.get_stalled_jobs(
            queue=ARCHIVE_QUEUE,
            task_name="archive",
            seconds_since_heartbeat=STALLED_HEARTBEAT_SECONDS,
        )
        return [(j.id, j.task_kwargs["job_id"]) for j in found if j.id is not None]

    async def finish(queue_job_id: int) -> None:
        await app.job_manager.finish_job_by_id_async(
            queue_job_id, status=Status.FAILED, delete_job=False
        )

    await reclaim_stalled(default_sessionmaker(), settings, stalled=stalled, finish=finish)


async def record_profiles(settings: Settings) -> None:
    """The web pod lists the profiles from the table, without the mount."""
    async with default_sessionmaker()() as session:
        await cookies.record_profiles(session, cookies.profiles(settings), datetime.now(UTC))
        await session.commit()


async def read_accounts(settings: Settings) -> None:
    await cookies.read_accounts(default_sessionmaker(), settings, reader=read_account)


async def detect_lives(settings: Settings) -> None:
    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        channels = await watched_channels(session)
        profile = await cookies.general(session)
    if not channels:
        return

    def member_probe(video_id: str, member: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="savetube-") as own:
            return probe_video(video_id, cookies.cookiefile(settings, member, Path(own)))

    # The probes take the general profile. The RSS feed needs no cookies.
    with tempfile.TemporaryDirectory(prefix="savetube-") as private:
        cookiefile = cookies.cookiefile(settings, profile, Path(private))
        # Every watched channel costs a Live tab listing and a `/live`
        # extraction per tick. The guest limit is near 300 requests an hour,
        # so this caps the number of watched channels.
        for channel_id in channels:
            await check_logged(
                sessionmaker,
                channel_id,
                fetch_feed=fetch_feed,
                probe=partial(probe_video, cookiefile=cookiefile),
                probe_live=partial(probe_live, cookiefile=cookiefile),
                list_streams=partial(list_streams, cookiefile=cookiefile),
                member_probe=member_probe,
                profiles=cookies.profiles(settings),
            )


async def recorders(settings: Settings) -> None:
    await start_recorders(
        default_sessionmaker(),
        datetime.now(UTC),
        recorders=lambda: k8s.list_recorder_jobs(settings.namespace),
        create=partial(k8s.create_recorder_job, settings.namespace, settings.recorder_cronjob),
    )


async def format_watch(settings: Settings, probes: asyncio.Lock) -> None:
    await watch_formats(
        default_sessionmaker(), settings, probe=probe_formats, tools=Tools(), lock=probes
    )


async def reencode(settings: Settings, lock: asyncio.Lock, skipped: set[str]) -> None:
    await encode.step(default_sessionmaker(), settings, tools=Tools(), lock=lock, skipped=skipped)


async def move_requested(
    settings: Settings, requests: scheduler_api.Requests, moves: asyncio.Lock
) -> None:
    """Take the user's Move now before the move, so one asked during a long
    move runs another after it: it may name a video staged meanwhile."""
    async with moves:
        await move_staged(default_sessionmaker(), settings, requested=requests.take_move())


async def _every(
    name: str,
    step: Callable[[], Awaitable[None]],
    seconds: float = TICK_SECONDS,
    wake: asyncio.Event | None = None,
) -> None:
    """Run `step` every `seconds`, and at once when `wake` is set, also while
    `step` runs."""
    while True:
        if wake is not None:
            wake.clear()
        try:
            await step()
        except Exception:
            emit(log, logging.ERROR, "loop.failed", loop=name, exc_info=True)
        if wake is None:
            await asyncio.sleep(seconds)
        else:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), seconds)


async def run(settings: Settings) -> None:
    emit(
        log,
        logging.INFO,
        "scheduler.start",
        tick=f"{TICK_SECONDS}s",
        dispatch=f"{DISPATCH_SECONDS}s",
        api=settings.scheduler_api_port,
    )
    # The format watch and the user's probes take turns: every listing counts
    # against YouTube's request limits.
    probes = asyncio.Lock()
    # The encode loop and a cancel from the API take turns on the job files.
    encodes = asyncio.Lock()
    # The staging move and a delete of a staged video take turns on its files.
    moves = asyncio.Lock()
    requests = scheduler_api.Requests()
    api = scheduler_api.create_app(
        settings,
        default_sessionmaker(),
        probes=probes,
        probe=probe_formats,
        tools=Tools(),
        requests=requests,
        encodes=encodes,
        moves=moves,
    )
    async with app.open_async():
        # Separate loops, so a long first listing of a big channel never delays
        # live detection or the storage sample.
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_every("storage sample", lambda: sample_storage(settings)))
            tg.create_task(_every("cookie profiles", lambda: record_profiles(settings)))
            # Its own loop, so a slow read never delays recording the profiles.
            tg.create_task(_every("cookie accounts", lambda: read_accounts(settings)))
            tg.create_task(
                _every(
                    "source poll",
                    lambda: poll_sources(settings, requests),
                    wake=requests.poll_wake,
                )
            )
            report = DispatchLog()
            tg.create_task(
                _every("archive dispatch", lambda: dispatch_archives(report), DISPATCH_SECONDS)
            )
            tg.create_task(_every("stalled job reclaim", lambda: reclaim(settings)))
            tg.create_task(
                _every(
                    "incoming cleanup",
                    lambda: clean_incoming(default_sessionmaker(), settings),
                    CLEANUP_SECONDS,
                )
            )
            if settings.staging_root is not None:
                tg.create_task(
                    _every(
                        "staging move",
                        lambda: move_requested(settings, requests, moves),
                        wake=requests.move_wake,
                    )
                )
            tg.create_task(
                _every(
                    "database cleanup",
                    lambda: clean_database(default_sessionmaker()),
                    CLEANUP_SECONDS,
                )
            )
            tg.create_task(_every("format watch", lambda: format_watch(settings, probes)))
            skipped: set[str] = set()
            tg.create_task(_every("re-encoding", lambda: reencode(settings, encodes, skipped)))
            tg.create_task(scheduler_api.serve(api, settings.scheduler_api_port))
            tg.create_task(_every("live detection", lambda: detect_lives(settings)))
            # Kubernetes sets this in every pod. A scheduler outside a cluster,
            # as in compose.yaml, has no API to make recorder Jobs with.
            if os.environ.get("KUBERNETES_SERVICE_HOST"):
                tg.create_task(
                    _every("recorder start", lambda: recorders(settings), RECORDER_SECONDS)
                )
