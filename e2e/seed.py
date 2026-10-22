"""Create, seed and drop the database of one frontend smoke run.

Usage, from the repo root with SAVETUBE_TEST_DATABASE_URL set:
  uv run --project backend python e2e/seed.py create <db name> <archive root> <manifest path>
  uv run --project backend python e2e/seed.py drop <db name>

The database name follows backend/tests/test_db.py, savetube_test_<unix seconds>_<hex>,
so that suite's setup also drops one a killed run leaves behind. The manifest is JSON
with the ids the smoke checks open. Nothing here prints the database URL.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from savetube.config import Settings
from savetube.db.migrate import migrate
from savetube.db.models import (
    Channel,
    DownloadSettings,
    Job,
    JobKind,
    JobState,
    Source,
    SourceKind,
    StorageSample,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import VideoType, channel_dir_name, video_dir
from sqlalchemy.orm import Session

DB_RE = re.compile(r"savetube_test_\d+_[0-9a-f]+")
NOW = datetime.now(UTC)

MAIN = "UCe2eMainChannel000000001"
OTHER = "UCe2eOtherChannel00000002"
PLAYABLE = "e2ePlay0001"


def test_dsn(name: str) -> str:
    """SAVETUBE_TEST_DATABASE_URL with its database swapped for `name`."""
    if not DB_RE.fullmatch(name):
        raise SystemExit(f"database name must match {DB_RE.pattern}")
    admin = os.environ.get("SAVETUBE_TEST_DATABASE_URL", "")
    if not admin:
        raise SystemExit("SAVETUBE_TEST_DATABASE_URL is unset")
    return urlunsplit(urlsplit(admin)._replace(path=f"/{name}"))


def _admin_engine() -> sa.Engine:
    admin = Settings(database_url=os.environ["SAVETUBE_TEST_DATABASE_URL"])
    return sa.create_engine(admin.sqlalchemy_url, isolation_level="AUTOCOMMIT")


def ffmpeg() -> str | None:
    """ffmpeg on PATH, else the binary of the imageio-ffmpeg wheel through uvx."""
    if found := shutil.which("ffmpeg"):
        return found
    try:
        out = subprocess.run(
            [
                "uvx",
                "--with",
                "imageio-ffmpeg",
                "python",
                "-I",
                "-c",
                "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def make_media(directory: Path, video_id: str) -> bool:
    """A 2 s H.264/Opus mkv, its Opus audio copy and an English vtt. False without ffmpeg."""
    exe = ffmpeg()
    if exe is None:
        return False
    directory.mkdir(parents=True, exist_ok=True)
    mkv = directory / f"{video_id}.mkv"
    subprocess.run(
        [
            exe,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "libopus",
            str(mkv),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(
        [
            exe,
            "-y",
            "-i",
            str(mkv),
            "-vn",
            "-c:a",
            "copy",
            str(directory / f"{video_id}.opus"),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    (directory / f"{video_id}.en.vtt").write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nsmoke\n", encoding="utf-8"
    )
    return True


def seed(session: Session, archive_root: Path) -> dict:
    main_dir = channel_dir_name("E2E Main", MAIN)
    session.add_all(
        [
            Channel(
                id=MAIN,
                handle="@e2emain",
                name="E2E Main",
                dir_name=main_dir,
                watch_lives=True,
            ),
            Channel(
                id=OTHER,
                handle="@e2eother",
                name="E2E Other",
                dir_name=channel_dir_name("E2E Other", OTHER),
            ),
        ]
    )
    session.flush()

    base = "https://www.youtube.com/channel/"
    tab_videos = Source(
        url=f"{base}{MAIN}/videos",
        kind=SourceKind.TAB,
        channel_id=MAIN,
        archiving=True,
        last_polled_at=NOW - timedelta(minutes=30),
    )
    tab_streams = Source(
        url=f"{base}{MAIN}/streams",
        kind=SourceKind.TAB,
        channel_id=MAIN,
        archiving=True,
        last_polled_at=NOW - timedelta(minutes=30),
    )
    playlist = Source(
        url="https://www.youtube.com/playlist?list=PLe2eSmoke",
        kind=SourceKind.PLAYLIST,
        title="E2E Playlist",
        channel_id=MAIN,
        archiving=True,
        last_polled_at=NOW - timedelta(hours=1),
    )
    other_tab = Source(
        url=f"{base}{OTHER}/videos",
        kind=SourceKind.TAB,
        channel_id=OTHER,
        archiving=True,
        last_polled_at=NOW - timedelta(hours=2),
    )
    session.add_all([tab_videos, tab_streams, playlist, other_tab])
    session.flush()

    info = {
        "id": PLAYABLE,
        "title": "Playable smoke video",
        "upload_date": "20261001",
        "ext": "mkv",
        "channel": "E2E Main",
        "duration": 2,
        "webpage_url": f"https://www.youtube.com/watch?v={PLAYABLE}",
        "subtitles": {"en": [{"ext": "vtt", "name": "English"}]},
    }
    playable_dir = video_dir(main_dir, VideoType.VIDEOS, info)
    media = make_media(archive_root / playable_dir, PLAYABLE)
    size = (
        sum(p.stat().st_size for p in (archive_root / playable_dir).iterdir())
        if media
        else 1000
    )

    def video(vid, channel, vtype, title, day, state=VideoState.COMPLETE, **kw):
        complete = state is VideoState.COMPLETE
        return Video(
            id=vid,
            channel_id=channel,
            type=vtype,
            title=title,
            upload_date=day,
            duration=kw.pop("duration", 600),
            state=state,
            live_status="was_live" if vtype is VideoType.LIVES else "not_live",
            size_bytes=kw.pop("size_bytes", 50_000_000) if complete else None,
            completed_at=NOW - timedelta(days=kw.pop("age_days", 2))
            if complete
            else None,
            **kw,
        )

    videos = [
        video(
            PLAYABLE,
            MAIN,
            VideoType.VIDEOS,
            info["title"],
            date(2026, 10, 1),
            duration=2,
            size_bytes=size,
            age_days=1,
            dir_path=str(playable_dir),
            info=info,
            verified_at=NOW - timedelta(days=1),
            verified_checks=["size", "probe", "packets"],
        ),
        video(
            "e2eVideo002",
            MAIN,
            VideoType.VIDEOS,
            "Second upload",
            date(2026, 9, 20),
            age_days=10,
        ),
        video(
            "e2eShort001",
            MAIN,
            VideoType.SHORTS,
            "A short",
            date(2026, 9, 25),
            duration=40,
        ),
        video(
            "e2eLive0001",
            MAIN,
            VideoType.LIVES,
            "Past stream",
            date(2026, 9, 18),
            duration=7200,
            age_days=20,
        ),
        video(
            "e2eMembr001",
            MAIN,
            VideoType.MEMBERS,
            "Members stream",
            date(2026, 9, 15),
            duration=3600,
            age_days=25,
        ),
        video(
            "e2ePend0001",
            MAIN,
            VideoType.VIDEOS,
            "Waiting upload",
            date(2026, 10, 15),
            state=VideoState.PENDING,
        ),
        video(
            "e2eRun00001",
            MAIN,
            VideoType.VIDEOS,
            "Downloading upload",
            date(2026, 10, 16),
            state=VideoState.DOWNLOADING,
        ),
        video(
            "e2eFail0001",
            MAIN,
            VideoType.VIDEOS,
            "Broken upload",
            date(2026, 10, 10),
            state=VideoState.FAILED,
        ),
        video(
            "e2eRec00001",
            MAIN,
            VideoType.LIVES,
            "Upcoming stream",
            None,
            state=VideoState.PENDING,
        ),
        video(
            "e2eOther001",
            OTHER,
            VideoType.VIDEOS,
            "Other channel upload",
            date(2026, 9, 30),
            age_days=5,
        ),
    ]
    session.add_all(videos)
    session.flush()
    for v in videos:
        if v.channel_id == MAIN:
            source = tab_streams if v.type is VideoType.LIVES else tab_videos
            session.add(VideoSource(video_id=v.id, source_id=source.id))
    session.add(VideoSource(video_id="e2eVideo002", source_id=playlist.id))
    session.add(VideoSource(video_id="e2eOther001", source_id=other_tab.id))

    session.add_all(
        [
            Job(kind=JobKind.ARCHIVE, state=JobState.QUEUED, video_id="e2ePend0001"),
            Job(
                kind=JobKind.ARCHIVE,
                state=JobState.RUNNING,
                video_id="e2eRun00001",
                attempts=1,
                started_at=NOW - timedelta(minutes=3),
                progress={
                    "format": "299",
                    "pct": 42.0,
                    "done": 4_200_000,
                    "total": 10_000_000,
                    "speed": 1_000_000,
                    "eta": 6,
                },
            ),
            Job(
                kind=JobKind.ARCHIVE,
                state=JobState.FAILED,
                video_id="e2eFail0001",
                attempts=3,
                log_tail="ERROR: [youtube] e2eFail0001: Video unavailable",
                started_at=NOW - timedelta(hours=2),
                finished_at=NOW - timedelta(hours=2),
            ),
            Job(
                kind=JobKind.ARCHIVE,
                state=JobState.SUCCEEDED,
                video_id=PLAYABLE,
                attempts=1,
                started_at=NOW - timedelta(days=1, minutes=5),
                finished_at=NOW - timedelta(days=1),
            ),
            Job(
                kind=JobKind.RECORD,
                state=JobState.QUEUED,
                video_id="e2eRec00001",
                scheduled_at=NOW + timedelta(hours=5),
            ),
            Job(
                kind=JobKind.RECORD,
                state=JobState.SUCCEEDED,
                video_id="e2eLive0001",
                started_at=NOW - timedelta(days=20, hours=2),
                finished_at=NOW - timedelta(days=20),
            ),
        ]
    )
    # A migration inserts the row with the defaults.
    session.merge(
        DownloadSettings(
            id=1,
            max_concurrent=2,
            rate_limit_bytes=5_000_000,
            window_start=time(1, 0),
            window_end=time(7, 0),
            window_enabled=False,
            daily_limit=20,
        )
    )
    session.add(
        StorageSample(
            sampled_at=NOW, total_bytes=4_000_000_000_000, free_bytes=1_500_000_000_000
        )
    )
    return {"channel_id": MAIN, "video_id": PLAYABLE, "media": media}


def create(name: str, archive_root: Path, manifest: Path) -> None:
    dsn = test_dsn(name)
    engine = _admin_engine()
    with engine.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {name}"))
    engine.dispose()
    settings = Settings(database_url=dsn, archive_root=archive_root)
    migrate(settings)
    engine = sa.create_engine(settings.sqlalchemy_url)
    with Session(engine) as session, session.begin():
        result = seed(session, archive_root)
    engine.dispose()
    manifest.write_text(json.dumps(result))


def drop(name: str) -> None:
    test_dsn(name)
    engine = _admin_engine()
    with engine.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    engine.dispose()


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["create", name, archive, manifest]:
            create(name, Path(archive), Path(manifest))
        case ["drop", name]:
            drop(name)
        case _:
            raise SystemExit(__doc__)
