import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL, _api, _sql
from fastapi.testclient import TestClient

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)
from savetube.layout import VideoType
from savetube.pipeline import store
from savetube.web.app import create_app
from savetube.web.auth import User, current_user

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


INFO = {
    "id": "dQw4w9WgXcQ",
    "title": "첫 방송 / part 1",
    "channel_id": "UC1",
    "channel": "Renamed Channel",
    "upload_date": "20260315",
    "duration": 3600,
    "live_status": "was_live",
    "availability": "public",
    "format_id": "299+251",
}


@pytest.mark.anyio
async def test_store_moves_download_and_records_video(settings):
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(Channel(id="UC1", name="Original", dir_name="Original [UC1]"))
        session.add(Video(id=INFO["id"], channel_id="UC1", type=VideoType.VIDEOS, title="?"))
        # No relationship() orders these inserts, so the FK targets go first.
        await session.flush()
        job = Job(kind=JobKind.RECORD, video_id=INFO["id"])
        session.add(job)
        await session.commit()
        job_id = job.id

    incoming = settings.incoming_root / str(job_id)
    (incoming / ".tmp").mkdir(parents=True)
    (incoming / ".tmp" / "frag1").write_bytes(b"leftover")
    (incoming / "dQw4w9WgXcQ.mkv").write_bytes(b"x" * 1000)
    (incoming / "dQw4w9WgXcQ.info.json").write_bytes(b"{}")

    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        await store(
            session, job=job, info=INFO, incoming=incoming, archive_root=settings.archive_root
        )
        await session.commit()

    # The directory name was fixed at first add; the YouTube rename does not move it.
    final = (
        settings.archive_root
        / "Original [UC1]"
        / "lives"
        / "2026-03-15 첫 방송 ⧸ part 1 [dQw4w9WgXcQ]"
    )
    assert sorted(p.name for p in final.iterdir()) == ["dQw4w9WgXcQ.info.json", "dQw4w9WgXcQ.mkv"]
    assert not incoming.exists()

    async with sessionmaker() as session:
        video = await session.get(Video, INFO["id"])
        job = await session.get(Job, job_id)
        channel = await session.get(Channel, "UC1")
        assert video.state is VideoState.COMPLETE
        assert video.type is VideoType.LIVES
        assert video.size_bytes == 1002
        assert video.dir_path == "Original [UC1]/lives/2026-03-15 첫 방송 ⧸ part 1 [dQw4w9WgXcQ]"
        assert video.info["format_id"] == "299+251"
        assert job.state is JobState.SUCCEEDED and job.finished_at is not None
        assert channel.dir_name == "Original [UC1]"
    await engine.dispose()


@pytest.mark.anyio
async def test_store_drops_fragment_lists_and_download_records_from_the_info(settings):
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(Channel(id="UC1", name="A", dir_name="A [UC1]"))
        await session.flush()
        job = Job(kind=JobKind.RECORD, video_id=None)
        session.add(job)
        await session.commit()
        job_id = job.id

    incoming = settings.incoming_root / str(job_id)
    incoming.mkdir(parents=True)
    (incoming / "dQw4w9WgXcQ.mkv").write_bytes(b"x")
    fragment = {"url": "https://rr1.googlevideo.com/sq/1", "duration": 5.0}
    info = {
        **INFO,
        "formats": [
            {"format_id": "251", "acodec": "opus", "vcodec": "none", "fragments": [fragment]},
            {"format_id": "299", "acodec": "none", "vcodec": "avc1", "url": "https://x/299"},
        ],
        "requested_formats": [
            {"format_id": "299", "vcodec": "avc1", "fragments": [fragment]},
            {"format_id": "251", "acodec": "opus"},
        ],
        "requested_downloads": [{"filepath": str(incoming / "dQw4w9WgXcQ.mkv")}],
    }

    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        await store(
            session, job=job, info=info, incoming=incoming, archive_root=settings.archive_root
        )
        await session.commit()

    async with sessionmaker() as session:
        stored = (await session.get(Video, INFO["id"])).info
    assert stored == {
        **INFO,
        "formats": [
            {"format_id": "251", "acodec": "opus", "vcodec": "none"},
            {"format_id": "299", "acodec": "none", "vcodec": "avc1", "url": "https://x/299"},
        ],
        "requested_formats": [
            {"format_id": "299", "vcodec": "avc1"},
            {"format_id": "251", "acodec": "opus"},
        ],
    }
    await engine.dispose()


def test_api_reports_only_stored_videos(settings):
    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.begin() as c:
        c.execute(
            sa.text("INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')")
        )
        c.execute(
            sa.text(
                "INSERT INTO source (url, kind, channel_id, enabled, poll_interval_minutes)"
                " VALUES ('https://www.youtube.com/@a/videos', 'tab', 'UC1', true, 360)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO video"
                " (id, channel_id, type, title, duration, size_bytes, state, completed_at) VALUES"
                "         ('v1', 'UC1', 'videos', 'alpha_1', 60, 100, 'complete', now()),"
                "        ('v2', 'UC1', 'lives', 'beta', 120, 300, 'complete', now()),"
                "        ('v3', 'UC1', 'videos', 'gamma', 999, 999, 'pending', NULL)"
            )
        )
        c.execute(
            sa.text(
                "INSERT INTO storage_sample"
                " VALUES (now() - interval '1 hour', 10, 4), (now(), 10, 3)"
            )
        )
    engine.dispose()

    app = create_app(settings)
    app.dependency_overrides[current_user] = lambda: User(sub="u")
    with TestClient(app) as client:
        summary = client.get("/api/stats/summary").json()
        assert summary["total_bytes"] == 400
        assert summary["video_count"] == 2
        assert summary["duration_seconds"] == 180
        assert summary["free_bytes"] == 3
        assert sorted((t["type"], t["bytes"], t["count"]) for t in summary["by_type"]) == [
            ("lives", 300, 1),
            ("videos", 100, 1),
        ]
        assert summary["by_channel"] == [
            {"channel_id": "UC1", "name": "A", "bytes": 400, "count": 2}
        ]

        daily = client.get("/api/stats/daily").json()
        assert [(d["bytes"], d["count"]) for d in daily] == [(400, 2)]

        assert client.get("/api/channels").json()[0]["video_count"] == 2

        page = client.get("/api/videos", params={"type": "videos"}).json()
        assert page["total"] == 2
        # `_` in the query is literal, not a LIKE wildcard.
        assert client.get("/api/videos", params={"q": "a_1"}).json()["total"] == 1
        assert client.get("/api/videos", params={"q": "a_"}).json()["total"] == 1
        assert client.get("/api/videos/v2").json()["type"] == "lives"
        assert client.get("/api/videos/nope").status_code == 404


def test_api_daily_stats_bucket_by_the_seoul_day(settings):
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, size_bytes, state, completed_at) VALUES"
        # 2026-10-21 01:30 and 23:00 in Seoul, two UTC days apart.
        " ('v1', 'UC1', 'videos', 'a', 100, 'complete', '2026-10-20T16:30:00Z'),"
        " ('v2', 'UC1', 'videos', 'b', 20, 'complete', '2026-10-21T14:00:00Z'),"
        # 2026-10-22 00:10 in Seoul, still 10-21 in UTC.
        " ('v3', 'UC1', 'videos', 'c', 3, 'complete', '2026-10-21T15:10:00Z')",
    )
    with _api(settings) as client:
        daily = client.get("/api/stats/daily", params={"days": 3650}).json()
    assert daily == [
        {"day": "2026-10-21", "bytes": 120, "count": 2},
        {"day": "2026-10-22", "bytes": 3, "count": 1},
    ]


FINAL_DIR = "Original [UC1]/lives/2026-03-15 첫 방송 ⧸ part 1 [dQw4w9WgXcQ]"


async def _store_setup(settings, video_state):
    """Channel UC1 and job 1 for INFO's video, its row in `video_state` (None: no row).
    Returns the sessionmaker, the engine, the job id and a filled download dir."""
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(Channel(id="UC1", name="Original", dir_name="Original [UC1]"))
        if video_state is not None:
            session.add(
                Video(
                    id=INFO["id"],
                    channel_id="UC1",
                    type=VideoType.VIDEOS,
                    title="?",
                    state=video_state,
                )
            )
        await session.flush()
        job = Job(kind=JobKind.RECORD, video_id=INFO["id"] if video_state is not None else None)
        session.add(job)
        await session.commit()
        job_id = job.id

    incoming = settings.incoming_root / str(job_id)
    incoming.mkdir(parents=True)
    (incoming / "dQw4w9WgXcQ.mkv").write_bytes(b"n" * 100)
    (incoming / "dQw4w9WgXcQ.info.json").write_bytes(b"{}")
    return engine, sessionmaker, job_id, incoming


async def _store(settings, sessionmaker, job_id, incoming, ready_root):
    async with sessionmaker() as session:
        job = await session.get(Job, job_id)
        await store(
            session,
            job=job,
            info=INFO,
            incoming=incoming,
            archive_root=settings.archive_root,
            ready_root=ready_root,
        )
        await session.commit()


def _stale_ready(settings):
    ready_root = settings.archive_root.parent / "staging" / "ready"
    stale = ready_root / INFO["id"]
    stale.mkdir(parents=True)
    (stale / "dQw4w9WgXcQ.mkv").write_bytes(b"o" * 7)
    (stale / "dQw4w9WgXcQ.f299.mp4.part").write_bytes(b"o" * 3)
    return ready_root, stale


@pytest.mark.anyio
@pytest.mark.parametrize(
    "video_state",
    [VideoState.PENDING, VideoState.DOWNLOADING, VideoState.FAILED, None],
    ids=["pending", "downloading", "failed", "no-row"],
)
async def test_store_replaces_a_ready_dir_a_dead_attempt_left_behind(settings, video_state):
    """An attempt renamed its download into ready/<id>/, then its commit failed:
    the row never became staged, and every retry hit FileExistsError."""
    ready_root, stale = _stale_ready(settings)
    engine, sessionmaker, job_id, incoming = await _store_setup(settings, video_state)

    await _store(settings, sessionmaker, job_id, incoming, ready_root)

    assert sorted(p.name for p in stale.iterdir()) == ["dQw4w9WgXcQ.info.json", "dQw4w9WgXcQ.mkv"]
    assert (stale / "dQw4w9WgXcQ.mkv").read_bytes() == b"n" * 100
    assert not incoming.exists()
    async with sessionmaker() as session:
        video = await session.get(Video, INFO["id"])
        assert video.state is VideoState.STAGED
    await engine.dispose()


@pytest.mark.anyio
async def test_store_keeps_a_ready_dir_whose_video_is_staged(settings):
    ready_root, stale = _stale_ready(settings)
    engine, sessionmaker, job_id, incoming = await _store_setup(settings, VideoState.STAGED)

    with pytest.raises(FileExistsError):
        await _store(settings, sessionmaker, job_id, incoming, ready_root)

    assert sorted(p.name for p in stale.iterdir()) == [
        "dQw4w9WgXcQ.f299.mp4.part",
        "dQw4w9WgXcQ.mkv",
    ]
    assert (stale / "dQw4w9WgXcQ.mkv").read_bytes() == b"o" * 7
    await engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("staged", [False, True], ids=["archive", "staged"])
async def test_store_refuses_a_place_that_exists_in_the_archive_tree(settings, staged):
    ready_root = settings.archive_root.parent / "staging" / "ready" if staged else None
    if ready_root is not None:
        ready_root.mkdir(parents=True)
    taken = settings.archive_root / FINAL_DIR
    taken.mkdir(parents=True)
    (taken / "keep.mkv").write_bytes(b"k")
    engine, sessionmaker, job_id, incoming = await _store_setup(settings, VideoState.PENDING)

    with pytest.raises(FileExistsError):
        await _store(settings, sessionmaker, job_id, incoming, ready_root)

    assert [p.name for p in taken.iterdir()] == ["keep.mkv"]
    if ready_root is not None:
        assert not (ready_root / INFO["id"]).exists()
    await engine.dispose()
