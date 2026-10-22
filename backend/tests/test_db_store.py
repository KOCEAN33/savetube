import pytest
import sqlalchemy as sa
from conftest import ADMIN_URL
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
