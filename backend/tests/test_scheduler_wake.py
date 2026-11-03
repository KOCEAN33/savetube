import asyncio
import contextlib

import pytest

from savetube import scheduler
from savetube.config import Settings

# The loops scheduler.run starts besides the staging move. Each is faked to do nothing.
_OTHER_STEPS = [
    "sample_storage",
    "record_profiles",
    "read_accounts",
    "poll_sources",
    "dispatch_archives",
    "reclaim",
    "clean_incoming",
    "clean_database",
    "format_watch",
    "reencode",
    "detect_lives",
    "recorders",
]


async def _settle():
    """Lets every ready task run, so a step that would run again has run."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_the_staging_loop_moves_at_once_on_a_request_and_again_for_one_made_during_a_move(
    monkeypatch, tmp_path
):
    created = []
    moves = []
    third = asyncio.Event()

    async def nothing(*args, **kwargs):
        pass

    async def serve(api, port):
        await asyncio.Event().wait()

    def create_app(settings, sessionmaker, **kwargs):
        created.append(kwargs["requests"])

    async def move_staged(sessionmaker, settings, *, requested=False):
        moves.append(requested)
        if len(moves) == 2:
            # The user asks again while this move runs.
            created[0].ask_move()
        if len(moves) == 3:
            third.set()

    for name in _OTHER_STEPS:
        monkeypatch.setattr(scheduler, name, nothing)
    monkeypatch.setattr(scheduler, "move_staged", move_staged)
    monkeypatch.setattr(scheduler, "default_sessionmaker", lambda: None)
    monkeypatch.setattr(scheduler.scheduler_api, "create_app", create_app)
    monkeypatch.setattr(scheduler.scheduler_api, "serve", serve)
    monkeypatch.setattr(scheduler.app, "open_async", contextlib.nullcontext)
    settings = Settings(database_url="postgres://x@127.0.0.1/x", staging_root=tmp_path)
    run = asyncio.create_task(scheduler.run(settings))
    try:
        await _settle()
        assert moves == [False]
        created[0].ask_move()
        await asyncio.wait_for(third.wait(), 5)
        await _settle()
        assert moves == [False, True, True]
    finally:
        run.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run
