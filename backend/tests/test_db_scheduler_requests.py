"""User actions the web asks the scheduler to do now, over its internal API
(docs/plan/runtime.md, Scheduler API): Poll now, archiving on, Move now."""

import asyncio
import logging

import httpx2
import pytest
from conftest import ADMIN_URL, _api, _events, _fields, _sql

from savetube.db import make_engine, make_sessionmaker
from savetube.integrity import Tools
from savetube.scheduler_api import Requests, create_app

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


def _never_probe(video_id, cookiefile):
    pytest.fail("no probe expected")


def _sources(settings):
    """1 and 2 due, 3 and 4 polled a minute ago, 5 disabled, 6 a playlist of
    the disabled `playlists` source 7."""
    _sql(
        settings,
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, last_polled_at)"
        " VALUES (1, 'https://www.youtube.com/@a/videos', 'tab', true, 360, NULL),"
        " (2, 'https://www.youtube.com/@b/videos', 'tab', true, 360, now() - interval '7 hours'),"
        " (3, 'https://www.youtube.com/@c/videos', 'tab', true, 360, now() - interval '1 minute'),"
        " (4, 'https://www.youtube.com/@d/videos', 'tab', true, 360, now() - interval '1 minute'),"
        " (5, 'https://www.youtube.com/@e/videos', 'tab', false, 360, NULL),"
        " (7, 'https://www.youtube.com/@g/playlists', 'playlists', false, 360, NULL)",
        "INSERT INTO source (id, url, kind, enabled, poll_interval_minutes, parent_id)"
        " VALUES (6, 'https://www.youtube.com/playlist?list=PL6', 'playlist', true, 360, 7)",
        "SELECT setval('source_id_seq', 7)",
    )


# The scheduler's API ---------------------------------------------------------


async def _scheduler_post(settings, path, requests):
    engine = make_engine(settings.sqlalchemy_url)
    app = create_app(
        settings,
        make_sessionmaker(engine),
        probes=asyncio.Lock(),
        probe=_never_probe,
        tools=Tools(),
        requests=requests,
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://scheduler"
    ) as client:
        r = await client.post(path)
    await engine.dispose()
    return r


@pytest.mark.anyio
async def test_scheduler_api_queues_a_poll_of_an_enabled_source(settings):
    _sources(settings)
    requests = Requests()

    r = await _scheduler_post(settings, "/poll/3", requests)

    assert r.status_code == 202
    assert requests.poll_wake.is_set()
    assert requests.take_poll() == 3
    assert requests.take_poll() is None


@pytest.mark.anyio
@pytest.mark.parametrize(("source_id", "status"), [(99, 404), (5, 409), (6, 409)])
async def test_scheduler_api_refuses_a_poll_of_a_missing_or_disabled_source(
    settings, source_id, status
):
    _sources(settings)
    requests = Requests()

    r = await _scheduler_post(settings, f"/poll/{source_id}", requests)

    assert r.status_code == status
    assert requests.take_poll() is None and not requests.poll_wake.is_set()


@pytest.mark.anyio
async def test_scheduler_api_asks_for_a_move_when_staging_is_set(settings):
    staged = settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})
    requests = Requests()

    r = await _scheduler_post(staged, "/staging/move", requests)

    assert r.status_code == 202
    assert requests.move_wake.is_set() and requests.take_move() is True
    assert requests.take_move() is False


@pytest.mark.anyio
async def test_scheduler_api_refuses_a_move_without_staging(settings):
    requests = Requests()

    r = await _scheduler_post(settings, "/staging/move", requests)

    assert r.status_code == 409
    assert requests.take_move() is False and not requests.move_wake.is_set()


# The poll loop ---------------------------------------------------------------


def _listing(url):
    handle = url.split("@")[1].split("/")[0]
    return {"channel_id": f"UC{handle}", "channel": handle, "uploader_id": f"@{handle}",
            "entries": []}  # fmt: skip


async def _poll_round(settings, monkeypatch, requests, during=None):
    """Runs one poll round. Returns the listed URLs' handles in order.
    `during(handle)` runs inside each listing."""
    from savetube import scheduler

    listed = []

    def list_entries(url, cookiefile):
        handle = url.split("@")[1].split("/")[0]
        listed.append(handle)
        if during is not None:
            during(handle)
        return _listing(url)

    engine = make_engine(settings.sqlalchemy_url)
    monkeypatch.setattr(scheduler, "default_sessionmaker", lambda: make_sessionmaker(engine))
    monkeypatch.setattr(scheduler, "list_entries", list_entries)
    await scheduler.poll_sources(settings, requests)
    await engine.dispose()
    return listed


@pytest.mark.anyio
async def test_a_requested_source_polls_first_even_when_not_due_and_once_per_round(
    settings, monkeypatch, caplog
):
    _sources(settings)
    requests = Requests()
    requests.ask_poll(3)
    requests.ask_poll(2)

    with caplog.at_level(logging.INFO, logger="savetube"):
        listed = await _poll_round(settings, monkeypatch, requests)

    assert listed == ["c", "b", "a"]
    assert [_fields(r)["source"] for r in _events(caplog, "poll.requested")] == [3, 2]


@pytest.mark.anyio
async def test_a_poll_requested_during_a_round_goes_ahead_of_the_due_sources_left(
    settings, monkeypatch
):
    _sources(settings)
    requests = Requests()

    asked = []

    def during(handle):
        if handle == "a" and not asked:
            asked.append(handle)
            requests.ask_poll(4)
            requests.ask_poll(2)
            # The source this listing polls: once per round.
            requests.ask_poll(1)

    listed = await _poll_round(settings, monkeypatch, requests, during)

    assert listed == ["a", "d", "b"]
    # The next round polls it again, and starts at once.
    assert requests.take_poll() == 1 and requests.take_poll() is None
    assert requests.poll_wake.is_set()


# The web's routes ------------------------------------------------------------


class _Scheduler:
    """The scheduler's API behind the web's outbound httpx2 calls."""

    def __init__(self, settings):
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        from savetube.db import PSYCOPG_CONNECT_ARGS

        # NullPool: the web app's loop runs these calls, so no connection
        # outlives it.
        engine = create_async_engine(
            settings.sqlalchemy_url, connect_args=PSYCOPG_CONNECT_ARGS, poolclass=NullPool
        )
        self.requests = Requests()
        self.app = create_app(
            settings,
            make_sessionmaker(engine),
            probes=asyncio.Lock(),
            probe=_never_probe,
            tools=Tools(),
            requests=self.requests,
        )
        self.urls = []

    async def handle(self, request):
        self.urls.append(str(request.url))
        return await httpx2.ASGITransport(app=self.app).handle_async_request(request)


async def _unreachable(self, request):
    raise httpx2.ConnectError("connection refused", request=request)


def test_api_poll_now_asks_the_scheduler(settings, monkeypatch):
    _sources(settings)
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post("/api/sources/3/poll")

    assert r.status_code == 202 and r.json()["id"] == 3
    assert scheduler.urls == [f"{settings.scheduler_url}/poll/3"]
    assert scheduler.requests.take_poll() == 3


@pytest.mark.parametrize("source_id", [5, 6])
def test_api_poll_now_refuses_a_disabled_source_without_asking_the_scheduler(
    settings, monkeypatch, source_id
):
    _sources(settings)
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post(f"/api/sources/{source_id}/poll")

    assert r.status_code == 409 and scheduler.urls == []


def test_api_poll_now_shows_an_unreachable_scheduler(settings, monkeypatch):
    _sources(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _unreachable)

    with _api(settings) as api:
        r = api.post("/api/sources/3/poll")

    assert r.status_code == 502 and "unreachable" in r.json()["detail"]


def test_api_archiving_on_asks_the_scheduler_to_poll(settings, monkeypatch):
    _sources(settings)
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.patch("/api/sources/3", json={"archiving": True})
        off = api.patch("/api/sources/4", json={"archiving": False})

    assert r.status_code == 200 and r.json()["archiving"] is True
    assert off.status_code == 200
    assert scheduler.urls == [f"{settings.scheduler_url}/poll/3"]


def test_api_archiving_on_is_saved_when_the_scheduler_is_unreachable(settings, monkeypatch, caplog):
    _sources(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _unreachable)

    with caplog.at_level(logging.INFO, logger="savetube"), _api(settings) as api:
        r = api.patch("/api/sources/3", json={"archiving": True})
        after = {s["id"]: s["archiving"] for s in api.get("/api/sources").json()}

    assert r.status_code == 200 and r.json()["archiving"] is True
    assert after[3] is True
    [failed] = _events(caplog, "source.poll_request_failed")
    assert failed.levelno == logging.WARNING
    assert (_fields(failed)["source"], _fields(failed)["status"]) == (3, 502)


def test_api_move_now_asks_the_scheduler(settings, monkeypatch):
    staged = settings.model_copy(update={"staging_root": settings.archive_root.parent / "staging"})
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, size_bytes) VALUES"
        " ('s1', 'UC1', 'videos', 't', 'staged', 10)",
    )
    scheduler = _Scheduler(staged)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post("/api/staging/move")

    assert r.status_code == 200 and r.json() == {"count": 1, "bytes": 10}
    assert scheduler.urls == [f"{settings.scheduler_url}/staging/move"]
    assert scheduler.requests.take_move() is True


def test_api_move_now_passes_on_the_scheduler_refusal(settings, monkeypatch):
    scheduler = _Scheduler(settings)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", scheduler.handle)

    with _api(settings) as api:
        r = api.post("/api/staging/move")

    assert r.status_code == 409 and r.json()["detail"] == "no staging volume"


def test_api_move_now_shows_an_unreachable_scheduler(settings, monkeypatch):
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _unreachable)

    with _api(settings) as api:
        r = api.post("/api/staging/move")

    assert r.status_code == 502 and "unreachable" in r.json()["detail"]
