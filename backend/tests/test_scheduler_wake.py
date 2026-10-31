import asyncio

import pytest

from savetube import scheduler
from savetube.scheduler_api import Requests


async def _settle():
    """Lets every ready task run, so a step that would run again has run."""
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.anyio
async def test_a_loop_runs_its_step_again_at_once_when_woken():
    wake = asyncio.Event()
    calls = []
    ran = asyncio.Event()

    async def step():
        calls.append(len(calls))
        ran.set()

    loop = asyncio.create_task(scheduler._every("test", step, 3600, wake=wake))
    try:
        await asyncio.wait_for(ran.wait(), 5)
        ran.clear()
        await _settle()
        assert calls == [0]
        wake.set()
        await asyncio.wait_for(ran.wait(), 5)
        await _settle()
        assert calls == [0, 1]
    finally:
        loop.cancel()


@pytest.mark.anyio
async def test_the_staging_loop_moves_at_once_on_a_request_and_again_for_one_made_during_a_move(
    monkeypatch,
):
    requests = Requests()
    moves = []
    third = asyncio.Event()

    async def move_staged(sessionmaker, settings, *, requested=False):
        moves.append(requested)
        if len(moves) == 2:
            # The user asks again while this move runs.
            requests.ask_move()
        if len(moves) == 3:
            third.set()

    monkeypatch.setattr(scheduler, "move_staged", move_staged)
    monkeypatch.setattr(scheduler, "default_sessionmaker", lambda: None)
    loop = asyncio.create_task(
        scheduler._every(
            "staging move",
            lambda: scheduler.move_requested(None, requests, asyncio.Lock()),
            3600,
            wake=requests.move_wake,
        )
    )
    try:
        await _settle()
        assert moves == [False]
        requests.ask_move()
        await asyncio.wait_for(third.wait(), 5)
        await _settle()
        assert moves == [False, True, True]
    finally:
        loop.cancel()
