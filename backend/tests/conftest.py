"""Runs against a real Postgres. SAVETUBE_TEST_DATABASE_URL is an admin URL whose
role has CREATEDB. Each run creates and drops its own database,
savetube_test_<unix seconds>_<hex>, so concurrent runs against one server do not collide.
Setup also drops databases of that pattern older than 24 hours, which killed runs leave behind."""

import os
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from savetube.config import Settings
from savetube.db import make_engine, make_sessionmaker
from savetube.db.migrate import migrate
from savetube.db.models import (
    Job,
    JobKind,
    Source,
    Video,
)
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

ADMIN_URL = os.environ.get("SAVETUBE_TEST_DATABASE_URL", "")
TEST_DB = f"savetube_test_{int(time.time())}_{secrets.token_hex(6)}"
TEST_DB_RE = re.compile(r"savetube_test_(\d+)_[0-9a-f]+")
STALE_AFTER_S = 24 * 3600


@pytest.fixture(scope="session")
def test_dsn():
    admin = Settings(database_url=ADMIN_URL)
    engine = sa.create_engine(admin.sqlalchemy_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        query = r"SELECT datname FROM pg_database WHERE datname LIKE 'savetube\_test\_%'"
        names = c.execute(sa.text(query)).scalars().all()
        cutoff = time.time() - STALE_AFTER_S
        for name in names:
            m = TEST_DB_RE.fullmatch(name)
            if m and int(m.group(1)) < cutoff:
                c.execute(sa.text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        c.execute(sa.text(f"CREATE DATABASE {TEST_DB}"))
    yield urlunsplit(urlsplit(ADMIN_URL)._replace(path=f"/{TEST_DB}"))
    with engine.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    engine.dispose()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path, test_dsn):
    s = Settings(
        database_url=test_dsn,
        archive_root=tmp_path / "archive",
        session_secret="s",
        oidc_client_id="c",
        oidc_issuer="https://issuer.test",
        public_url="http://testserver",
    )
    engine = sa.create_engine(s.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(sa.text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    engine.dispose()
    migrate(s)
    return s


def _sql(settings, *statements):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        for s in statements:
            c.execute(sa.text(s))
    engine.dispose()


NASA = "UCLA_DiR1FfKNvjuUpBHmylQ"
UPCOMING = {
    "id": "pAKqJxuxDgk",
    "title": "Artemis Acceleration (Oct. 23, 2026)",
    "channel_id": NASA,
    "channel": "NASA",
    "live_status": "is_upcoming",
    "media_type": "livestream",
    "release_timestamp": 1792778400,
}
LIVE_NOW = {
    "id": "M3HKLzjvKPc",
    "title": "Live Video from the International Space Station (Official NASA Stream)",
    "channel_id": NASA,
    "channel": "NASA",
    "live_status": "is_live",
    "media_type": "livestream",
    "release_timestamp": 1785417281,
}


def _feed(*ids):
    entries = "".join(f"<entry><yt:videoId>{i}</yt:videoId></entry>" for i in ids)
    return (
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"'
        f' xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'
    )


def _not_live(channel_id):
    from yt_dlp.utils import DownloadError

    raise DownloadError("ERROR: [youtube:tab] @NASAGoddard: The channel is not currently live")


def _no_streams(channel_id):
    return {"entries": []}


def _watch(settings, channel_id=NASA):
    """A record job needs a channel watching lives and an enabled tab source of it."""
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name, watch_lives) VALUES"
        f" ('{channel_id}', 'NASA', 'NASA [{channel_id}]', true)",
        "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes) VALUES"
        f" ('https://www.youtube.com/channel/{channel_id}/streams', 'tab', '{channel_id}',"
        "  true, 360)",
    )


async def _record_jobs(sessionmaker):
    async with sessionmaker() as session:
        return list(await session.scalars(sa.select(Job).where(Job.kind == JobKind.RECORD)))


VESP = "UCPd0Z22gF43dUidPYEQO5-w"
UTAWAKU = "【歌枠】ちょっと疲れちゃってるんだと思うよ【VESPERBELL ヨミ】"
GUERRILLA = "【ゲリラ(豪雨)】私魔女のキキ(危機)、こっちは配信すっぽかし女の【VESPERBELL カスカ】"
# Trimmed from `yt-dlp --flat-playlist -J` on VESPERBELL's /playlists tab, 2026-10.
PLAYLISTS_LISTING = {
    "id": VESP,
    "channel": "VESPERBELL",
    "channel_id": VESP,
    "uploader_id": "@VESPERBELL",
    "entries": [
        {
            "_type": "url",
            "ie_key": "YoutubeTab",
            "id": f"PLu4d2yplfpFd{n}",
            "url": f"https://www.youtube.com/playlist?list=PLu4d2yplfpFd{n}",
            "title": title,
            "channel": "Playlist",
            "channel_id": VESP,
        }
        for n, title in (("A", "◀︎YOMI Stream▶︎"), ("B", "◀︎KASUKA Stream▶︎"), ("C", "◀︎BD LIVE▶︎"))
    ],
}


def _api(settings, lister=None):
    from savetube.web.api import get_scanner

    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    if lister is not None:
        app.dependency_overrides[get_scanner] = lambda: lister
    return TestClient(app, headers={"X-Requested-With": "savetube"})


async def _cancel(queue_job_id):
    return True


def _no_probe(video_id, cookiefile):
    pytest.fail("no membership check expected")


async def _archive_jobs(sessionmaker):
    async with sessionmaker() as session:
        return sorted(
            await session.scalars(sa.select(Job.video_id).where(Job.kind == JobKind.ARCHIVE))
        )


PL = "https://www.youtube.com/playlist?list=PLu4d2yplfpFd"


def _parent(settings, *extra, enabled="true", other_channels="false"):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES"
        f" ('{VESP}', 'VESPERBELL', 'VESPERBELL [{VESP}]')",
        "INSERT INTO source (id, url, kind, channel_id, enabled, poll_interval_minutes,"
        " include_words, other_channels) VALUES"
        f" (1, 'https://www.youtube.com/channel/{VESP}/playlists', 'playlists', '{VESP}',"
        f"  {enabled}, 120, '{{歌枠}}', {other_channels})",
        "SELECT setval('source_id_seq', 1)",
        *extra,
    )


async def _poll(sessionmaker, settings, source_id, listing, cancel=_cancel, prober=_no_probe):
    from savetube.poll import poll_source

    await poll_source(
        sessionmaker,
        source_id,
        settings,
        lister=lambda u, c: listing,
        cancel=cancel,
        prober=prober,
    )


async def _children(sessionmaker):
    async with sessionmaker() as session:
        return list(
            await session.scalars(
                sa.select(Source).where(Source.parent_id == 1).order_by(Source.url)
            )
        )


MEMBERS_ONLY_ERROR = (
    "ERROR: [youtube] member: Join this channel to get access to members-only content"
    " like this video, and other exclusive perks."
)


def _job_rows(settings, *extra):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('v1', 'UC1', 'videos', 't', 'pending'), ('busy1', 'UC1', 'videos', 't', 'pending'),"
        " ('busy2', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts, queue_job_id) VALUES"
        " (1, 'archive', 'queued', 'v1', 0, 500)",
        *extra,
    )


def _closed_window_sql():
    """A window opening two hours from now, so now is outside it."""
    from savetube.tz import TIMEZONE

    now = datetime.now(TIMEZONE)
    start = (now + timedelta(hours=2)).strftime("%H:%M")
    end = (now + timedelta(hours=3)).strftime("%H:%M")
    return (
        f"UPDATE download_settings SET window_start = '{start}', window_end = '{end}',"
        " window_enabled = true"
    )


class _Runner:
    def __init__(self):
        self.params = None

    async def __call__(self, url, params, report):
        from savetube.jobs import DownloadFailed

        self.params = params
        raise DownloadFailed("stopped by the test")


async def _run(settings, *, live=False):
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    runner = _Runner()
    try:
        await run(1, settings, live=live, runner=runner, sessionmaker=sessionmaker)
    except Exception as e:
        if type(e).__name__ != "DownloadFailed":
            raise
    async with sessionmaker() as session:
        job = await session.get(Job, 1)
        queue_rows = await session.scalar(sa.text("SELECT count(*) FROM procrastinate_jobs"))
    await engine.dispose()
    return job, runner, queue_rows


def _queue_row(settings, status="todo"):
    """A procrastinate job row, as the real queue would add it."""
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        queue_job_id = c.execute(
            sa.text(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args)"
                " VALUES ('archive', 'archive', '{}') RETURNING id"
            )
        ).scalar()
        if status != "todo":
            c.execute(
                sa.text(
                    "UPDATE procrastinate_jobs SET status = CAST(:s AS procrastinate_job_status)"
                    " WHERE id = :id"
                ),
                {"s": status, "id": queue_job_id},
            )
    engine.dispose()
    return queue_job_id


async def _dispatch(settings, now=None):
    from savetube.jobs import dispatch

    enqueued = []

    async def enqueue(job_id):
        enqueued.append(job_id)
        return _queue_row(settings)

    engine = make_engine(settings.sqlalchemy_url)
    async with make_sessionmaker(engine)() as session:
        await dispatch(session, now or datetime.now(UTC), enqueue)
    async with make_sessionmaker(engine)() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
    await engine.dispose()
    return enqueued, jobs


def _waiting_rows(settings, *extra):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state) VALUES"
        " ('a', 'UC1', 'videos', 't', 'pending'), ('b', 'UC1', 'videos', 't', 'pending'),"
        " ('c', 'UC1', 'videos', 't', 'pending'), ('run', 'UC1', 'videos', 't', 'downloading'),"
        " ('live', 'UC1', 'lives', 't', 'downloading'), ('soon', 'UC1', 'lives', 't', 'pending')",
        "INSERT INTO job (id, kind, state, video_id, attempts) VALUES"
        " (10, 'archive', 'running', 'run', 1), (11, 'record', 'running', 'live', 1),"
        " (12, 'archive', 'queued', 'a', 0), (13, 'archive', 'queued', 'b', 0),"
        " (14, 'archive', 'queued', 'c', 0), (15, 'record', 'queued', 'soon', 0)",
        *extra,
    )


def _fields(record):
    return dict(getattr(record, "savetube_fields", {}))


class _Steps:
    """A runner that plays one step per call and records each call's params."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    async def __call__(self, url, params, report):
        self.calls.append(params)
        return await self.steps.pop(0)(params, report)


def _home(params):
    return Path(params["paths"]["home"])


def _part(params, name="v1.f251.webm.part", size=2048):
    tmp = _home(params) / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / name).write_bytes(b"x" * size)


AUDIO_INFO = {
    "id": "v1",
    "title": "t",
    "channel_id": "UC1",
    "channel": "A",
    "upload_date": "20260315",
    "duration": 60,
    "format_id": "251",
    "vcodec": "none",
    "acodec": "opus",
}


def _finished(stream_size=5, expected=5):
    """A step that downloads the whole audio file and reports its stream."""

    async def step(params, report):
        home = _home(params)
        home.mkdir(parents=True, exist_ok=True)
        media = home / "v1.webm"
        media.write_bytes(b"m" * stream_size)
        await report({"event": "format", "format_id": "251"})
        await report(
            {"event": "stream", "format_id": "251", "size": stream_size, "expected": expected}
        )
        return {**AUDIO_INFO, "requested_downloads": [{"filepath": str(media)}]}

    return step


class _FakeTools:
    """ffprobe that finds the expected audio stream, ffmpeg with no errors."""

    def __init__(self):
        self.packet_runs = 0

    def probe(self, path):
        return {"format": {"duration": "60.0"}, "streams": [{"codec_type": "audio"}]}

    def packets(self, path):
        self.packet_runs += 1
        return ""


async def _run_steps(settings, runner, tools=None):
    from savetube.jobs import run

    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    try:
        await run(
            1,
            settings,
            live=False,
            runner=runner,
            sessionmaker=sessionmaker,
            tools=tools or _FakeTools(),
        )
    except Exception as e:
        if type(e).__name__ != "DownloadFailed":
            raise
    async with sessionmaker() as session:
        jobs = {j.id: j for j in await session.scalars(sa.select(Job))}
        video = await session.get(Video, "v1")
    await engine.dispose()
    return jobs, video


def _events(caplog, name):
    return [r for r in caplog.records if r.getMessage() == name]


def _sql_rows(settings, statement):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        rows = c.execute(sa.text(statement)).all()
    engine.dispose()
    return rows


def _block_state(settings):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        row = c.execute(
            sa.text("SELECT block_count, blocked_until FROM download_settings WHERE id = 1")
        ).one()
    engine.dispose()
    return row.block_count, row.blocked_until
