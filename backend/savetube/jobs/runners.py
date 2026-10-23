"""The download child process and the runners that start a download."""

import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from savetube import logs
from savetube.logs import emit, human_bytes, human_duration
from savetube.ytdl import download

# Every module of the package logs as `savetube.jobs`.
log = logging.getLogger("savetube.jobs")

# The child sends progress at most this often, and the parent writes the job
# row at most every PROGRESS_WRITE_SECONDS.
PROGRESS_SEND_SECONDS = 1
PROGRESS_WRITE_SECONDS = 5
# How long the parent waits for the child's last messages after it exits.
PUMP_DRAIN_SECONDS = 10

# Takes what the download child sends: its progress (savetube.logs.progress),
# or an event, a dict with `event` set. `format` carries the chosen
# `format_id`. `stream` carries a downloaded stream's `format_id`, its `size`,
# and the format's exact `filesize` as `expected`, or None.
Report = Callable[[dict[str, Any]], Awaitable[None]]
Runner = Callable[[str, dict[str, Any], Report], Awaitable[dict[str, Any]]]


class DownloadFailed(Exception):
    pass


def _log_format(info: dict[str, Any]) -> None:
    size = info.get("filesize") or info.get("filesize_approx")
    emit(
        log,
        logging.INFO,
        "download.format",
        format=info.get("format_id"),
        res=info.get("resolution"),
        size_est=human_bytes(size) if size else None,
        duration=human_duration(info["duration"]) if info.get("duration") else None,
    )


def logged_download(
    url: str, params: dict[str, Any], send: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    """`download` with its log lines, and `send` fed the progress at most once a
    second. Builds the hooks where it runs, because `spawn` pickles arguments."""
    progress_log = logs.ProgressLog()
    last_send: float | None = None
    # Subtitles and the chat replay download first, through the same hook.
    # Progress counts only the media, which starts once the format is logged.
    media = False

    def on_format(info: dict[str, Any]) -> None:
        nonlocal media
        media = True
        _log_format(info)
        if send is not None:
            send({"event": "format", "format_id": info.get("format_id")})

    def on_progress(d: dict[str, Any]) -> None:
        nonlocal last_send
        if not media:
            return
        progress_log(d)
        if send is None:
            return
        if d.get("status") == "finished" and os.path.isfile(d.get("filename") or ""):
            # A merge deletes the stream files, so their sizes go to the parent now.
            fmt = d.get("info_dict") or {}
            send(
                {
                    "event": "stream",
                    "format_id": fmt.get("format_id"),
                    "size": os.path.getsize(d["filename"]),
                    "expected": fmt.get("filesize"),
                }
            )
            return
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if last_send is None or now - last_send >= PROGRESS_SEND_SECONDS:
            last_send = now
            send(logs.progress(d))

    def on_postprocessor(d: dict[str, Any]) -> None:
        if d.get("status") != "started":
            return
        step = d.get("postprocessor")
        merger = step == "Merger"
        emit(log, logging.INFO if merger else logging.DEBUG, "download.merge", step=step)
        if merger and send is not None:
            send({"step": "merging"})

    params = {**params, "progress_hooks": [on_progress], "postprocessor_hooks": [on_postprocessor]}
    return download(url, params, on_format=on_format)


def _child(
    url: str,
    params: dict[str, Any],
    out: str,
    level: str,
    fields: dict[str, Any],
    progress: Connection,
) -> None:
    # The parent decides how a download ends. A terminal's Ctrl-C reaches the
    # whole process group, and a stop must leave the parts for a resume.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    logs.configure(level)
    with logs.context(**fields):
        try:
            result = {"info": logged_download(url, params, progress.send)}
        except BaseException as e:  # noqa: BLE001 - reported to the parent, which decides
            result = {"error": f"{type(e).__name__}: {e}"}
    progress.close()
    Path(out).write_text(json.dumps(result))
    if "error" in result:
        raise SystemExit(1)


async def pump_progress(
    receive: Connection, report: Report, *, every: float = PROGRESS_WRITE_SECONDS
) -> None:
    """Pass every event from the child, and the latest progress at most once
    per `every` seconds, until the child closes the pipe. Reads on the event
    loop, so a download holds no thread for it."""
    loop = asyncio.get_running_loop()
    fd = receive.fileno()
    latest: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    closed = asyncio.Event()
    arrived = asyncio.Event()

    def readable() -> None:
        nonlocal latest
        try:
            message = receive.recv()
        except EOFError, OSError:
            closed.set()
            loop.remove_reader(fd)
        else:
            if "event" in message:
                events.append(message)
            else:
                latest = message
        arrived.set()

    async def write(message: dict[str, Any]) -> None:
        try:
            await report(message)
        except Exception as e:
            # Progress is for display. The download goes on without it.
            emit(log, logging.WARNING, "job.progress_failed", reason=f"{e}")

    loop.add_reader(fd, readable)
    try:
        while True:
            await arrived.wait()
            arrived.clear()
            while events:
                await write(events.pop(0))
            if latest is not None:
                update, latest = latest, None
                await write(update)
            if closed.is_set():
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(closed.wait(), every)
    finally:
        if not closed.is_set():
            loop.remove_reader(fd)


async def in_child(url: str, params: dict[str, Any], report: Report) -> dict[str, Any]:
    """Run one download in its own process.

    A Python thread cannot be stopped from outside, and a cancel or a hung
    download needs a process kill.
    """
    ctx = multiprocessing.get_context("spawn")
    level = logging.getLevelName(logging.getLogger("savetube").getEffectiveLevel())
    receive, send = ctx.Pipe(duplex=False)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "result.json"
        proc = ctx.Process(
            target=_child,
            args=(url, params, str(out), level, logs.current(), send),
            daemon=True,
        )
        proc.start()
        # The child holds the only write end, so its exit ends the pump.
        send.close()
        pump = asyncio.create_task(pump_progress(receive, report))
        try:
            await asyncio.to_thread(proc.join)
        except asyncio.CancelledError:
            # A kill leaves the `.part` files, and the retry resumes them.
            proc.kill()
            await asyncio.to_thread(proc.join)
            pump.cancel()
            raise
        finally:
            # The pump ends at the child's exit, after its last messages.
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(pump, PUMP_DRAIN_SECONDS)
            receive.close()
        result = json.loads(out.read_text()) if out.exists() else {}
        if proc.exitcode != 0 or "info" not in result:
            raise DownloadFailed(result.get("error") or f"exit code {proc.exitcode}")
        return result["info"]


def _live_progress(send: Callable[[dict[str, Any]], None]) -> Callable[[dict[str, Any]], None]:
    """`send` for a recording. A live from start downloads its video and audio
    streams at once, each with its own count, so `done` and `speed` become
    sums over the streams. Events stay out: the format pin is the archive
    resume's, which a VOD queued later must not inherit, and the stream sizes
    feed only the archive checks."""
    done: dict[str | None, int] = {}
    speed: dict[str | None, float] = {}

    def forward(message: dict[str, Any]) -> None:
        if "event" in message:
            return
        if "done" in message:
            fmt = message["format"]
            done[fmt] = message["done"] or 0
            speed[fmt] = message["speed"] or 0
            message = {
                **message,
                "format": "+".join(sorted(f for f in done if f)) or None,
                "done": sum(done.values()),
                "speed": sum(speed.values()) or None,
            }
        send(message)

    return forward


async def in_process(url: str, params: dict[str, Any], report: Report) -> dict[str, Any]:
    """A recorder's download, on a thread: its pod is the process. Progress
    reaches the job row through the pump an archive child uses."""
    receive, send = multiprocessing.Pipe(duplex=False)

    def run() -> dict[str, Any]:
        try:
            return logged_download(url, params, _live_progress(send.send))
        finally:
            # The pump ends when the write end closes.
            send.close()

    pump = asyncio.create_task(pump_progress(receive, report))
    try:
        return await asyncio.to_thread(run)
    finally:
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(pump, PUMP_DRAIN_SECONDS)
        receive.close()
