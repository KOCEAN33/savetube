import asyncio
import multiprocessing

import pytest

from savetube.jobs import pump_progress, youtube_block
from savetube.ytdl import network_error


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_the_pump_writes_the_latest_progress_at_most_once_per_interval():
    receive, send = multiprocessing.get_context("spawn").Pipe(duplex=False)
    written = []

    async def report(progress):
        written.append(progress)

    for pct in (1.0, 2.0, 3.0):
        send.send({"pct": pct})
    pump = asyncio.create_task(pump_progress(receive, report, every=0.3))
    await asyncio.sleep(0.1)
    # The first update is written at once, the rest wait for the interval.
    assert written == [{"pct": 1.0}]
    send.send({"pct": 4.0})
    send.close()
    await asyncio.wait_for(pump, 2)
    # The child's exit closes the pipe, and the last update still reaches the row.
    assert written == [{"pct": 1.0}, {"pct": 4.0}]
    receive.close()


@pytest.mark.anyio
async def test_the_pump_passes_every_event_in_order_and_ends_when_the_child_does():
    receive, send = multiprocessing.get_context("spawn").Pipe(duplex=False)
    written = []

    async def report(message):
        written.append(message)

    chosen = {"event": "format", "format_id": "137+140"}
    stream = {"event": "stream", "format_id": "137", "size": 10, "expected": 10}
    send.send(chosen)
    send.send({"pct": 1.0})
    send.send({"pct": 2.0})
    send.send(stream)
    send.close()
    # A long interval: the closed pipe must not wait for it.
    await asyncio.wait_for(pump_progress(receive, report, every=30), 2)
    # Events are never dropped for a newer progress update. Progress is.
    assert written == [chosen, stream, {"pct": 2.0}]
    receive.close()


@pytest.mark.anyio
async def test_a_failed_write_does_not_stop_the_pump():
    receive, send = multiprocessing.get_context("spawn").Pipe(duplex=False)
    calls = []

    async def report(progress):
        calls.append(progress)
        raise OSError("database gone")

    send.send({"pct": 1.0})
    send.close()
    await asyncio.wait_for(pump_progress(receive, report, every=0.05), 2)
    assert calls == [{"pct": 1.0}]
    receive.close()


@pytest.mark.parametrize(
    ("message", "block"),
    [
        ("DownloadError: ERROR: [youtube] v1: Sign in to confirm you’re not a bot.", True),
        ("ERROR: [youtube] v1: Sign in to confirm you're not a bot. Use --cookies", True),
        ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", True),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
        ("ERROR: [youtube] v1: Video unavailable. This video is not a bot test", False),
    ],
)
def test_a_youtube_block_is_the_bot_check_or_429(message, block):
    assert youtube_block(message) is block


@pytest.mark.parametrize(
    ("message", "network"),
    [
        (
            "DownloadError: ERROR: \r[download] Got error: HTTPSConnectionPool("
            "host='rr7---sn-3u-bh2zl.googlevideo.com', port=443): Read timed out."
            " (read timeout=20.0)",
            True,
        ),
        (
            "DownloadError: ERROR: \r[download] Got error:"
            " 4392287 bytes read, 5689076 more expected",
            True,
        ),
        ("ERROR: [download] Got error: [Errno 54] Connection reset by peer", True),
        ("ERROR: Remote end closed connection without response", True),
        ("ERROR: IncompleteRead(1024 bytes read, 2048 more expected)", True),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
        ("ERROR: [youtube] v1: Requested format is not available", False),
        ("ERROR: 4392287 bytes read", False),
    ],
)
def test_a_network_error_is_a_dropped_or_timed_out_connection(message, network):
    assert network_error(message) is network
