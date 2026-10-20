"""One download job, archive or recording, from the `job` row to the tree."""

import asyncio
import json
import multiprocessing
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from savetube.config import Settings
from savetube.db import default_sessionmaker
from savetube.db.models import Job, JobState, Video, VideoState
from savetube.pipeline import from_shorts_tab, prepare_incoming, rule_for, store
from savetube.ytdl import DEFAULT_FORMAT, build_params, copy_cookies, download, video_url

LOG_TAIL_CHARS = 4000

Runner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class DownloadFailed(Exception):
    pass


def _child(url: str, params: dict[str, Any], out: str) -> None:
    try:
        result = {"info": download(url, params)}
    except BaseException as e:  # noqa: BLE001 - reported to the parent, which decides
        result = {"error": f"{type(e).__name__}: {e}"}
    Path(out).write_text(json.dumps(result))
    if "error" in result:
        raise SystemExit(1)


async def in_child(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one download in its own process.

    A Python thread cannot be stopped from outside, and a cancel or a hung
    download needs a process kill.
    """
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "result.json"
        proc = ctx.Process(target=_child, args=(url, params, str(out)), daemon=True)
        proc.start()
        try:
            await asyncio.to_thread(proc.join)
        except asyncio.CancelledError:
            proc.kill()
            await asyncio.to_thread(proc.join)
            raise
        result = json.loads(out.read_text()) if out.exists() else {}
        if proc.exitcode != 0 or "info" not in result:
            raise DownloadFailed(result.get("error") or f"exit code {proc.exitcode}")
        return result["info"]


async def in_process(url: str, params: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(download, url, params)


async def _set_video_state(session: AsyncSession, video_id: str, state: VideoState) -> None:
    video = await session.get(Video, video_id)
    # A video stored by an earlier job stays complete through a re-download.
    if video is not None and video.state is not VideoState.COMPLETE:
        video.state = state


async def run(job_id: int, settings: Settings, *, live: bool, runner: Runner) -> None:
    sessionmaker = default_sessionmaker()
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        if job is None or job.video_id is None:
            raise LookupError(f"job {job_id} has no video")
        video_id = job.video_id
        rule = await rule_for(session, video_id)
        shorts = await from_shorts_tab(session, video_id)
        job.state = JobState.RUNNING
        job.attempts += 1
        job.started_at = datetime.now(UTC)
        await _set_video_state(session, video_id, VideoState.DOWNLOADING)
        await session.commit()

    # Any failure past RUNNING must reach the row, or the job stays running and
    # no poll queues the video again.
    try:
        incoming = prepare_incoming(settings.incoming_root, job_id)
        # Outside `incoming`, so the cookie jar never moves into the tree.
        with tempfile.TemporaryDirectory(prefix="savetube-") as private:
            cookiefile = None
            if rule and rule.use_cookies:
                if settings.cookies_file is None:
                    raise RuntimeError("the rule uses cookies and SAVETUBE_COOKIES_FILE is unset")
                cookiefile = copy_cookies(settings.cookies_file, Path(private))
            params = build_params(
                incoming,
                format=rule.format if rule else DEFAULT_FORMAT,
                cookiefile=cookiefile,
                live=live,
            )
            info = await runner(video_url(video_id), params)

        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            await store(
                session,
                job=job,
                info=info,
                incoming=incoming,
                archive_root=settings.archive_root,
                from_shorts_tab=shorts,
            )
            await session.commit()
    except Exception as e:
        # A retry is a new job with a new directory, so a partial download
        # would otherwise stay on the dataset for good.
        shutil.rmtree(settings.incoming_root / str(job_id), ignore_errors=True)
        async with sessionmaker() as session:
            job = await session.get(Job, job_id)
            job.state = JobState.FAILED
            job.finished_at = datetime.now(UTC)
            job.log_tail = (str(e) or type(e).__name__)[-LOG_TAIL_CHARS:]
            await _set_video_state(session, video_id, VideoState.FAILED)
            await session.commit()
        raise
