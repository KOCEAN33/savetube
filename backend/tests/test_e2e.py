"""End to end against a real Postgres: the web app under uvicorn and the worker
process (`savetube worker`), each in its own subprocess, driven over HTTP and
the archive queue.

The test touches public surfaces only, so refactors inside the app keep it
passing: HTTP routes, the `savetube` CLI, the procrastinate task named
`archive`, the database tables, and the archive tree on disk.

yt-dlp is faked at the library boundary: a `sitecustomize.py` on PYTHONPATH
replaces `yt_dlp.YoutubeDL` in every process of the app, including the
worker's `spawn` download children. No YouTube request is made.

With ffmpeg and ffprobe on PATH the media file is real, and the integrity
probe and the audio copy run. Without them both skip, as they do in the app.
"""

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx2
import pytest
import sqlalchemy as sa
from procrastinate import App, SyncPsycopgConnector
from sqlalchemy.orm import Session

from savetube.config import Settings
from savetube.db.migrate import migrate
from savetube.db.models import (
    Channel,
    Job,
    JobKind,
    JobState,
    ListingEntry,
    Source,
    SourceKind,
    Video,
    VideoSource,
    VideoState,
)
from savetube.layout import VideoType

ADMIN_URL = os.environ.get("SAVETUBE_TEST_DATABASE_URL", "")
TEST_DB = f"savetube_test_{int(time.time())}_{secrets.token_hex(6)}"

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")

CHANNEL_ID = "UCe2eChannel000000000001"
CHANNEL_URL = f"https://www.youtube.com/channel/{CHANNEL_ID}"
VIDEO_ID = "e2eVideo001"
TITLE = "E2E first upload"
PLAYLIST_ID = "PLe2ePlaylist01"
WRITE = {"X-Requested-With": "savetube"}
STARTUP_TIMEOUT_S = 60
JOB_TIMEOUT_S = 240
HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

# The info dict yt-dlp would return. The fake adds the id and the file paths.
INFO = {
    "title": TITLE,
    "channel_id": CHANNEL_ID,
    "channel": "E2E Channel",
    "uploader_id": "@e2e",
    "upload_date": "20260315",
    "duration": 2,
    "live_status": "not_live",
    "availability": "public",
    "format_id": "18",
    "vcodec": "mpeg4",
    "acodec": "opus",
    "subtitles": {"en": [{"ext": "vtt", "name": "English"}]},
}

# Replaces yt_dlp.YoutubeDL in each process started with its directory on
# PYTHONPATH. Python imports only the first `sitecustomize` it finds, so this
# one runs the one it shadows first.
FAKE_SITECUSTOMIZE = r"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.machinery.PathFinder.find_spec(
    "sitecustomize", [p for p in sys.path if os.path.abspath(p or ".") != _here]
)
if _spec is not None:
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))

import yt_dlp
from yt_dlp.utils import DownloadError

_Real = yt_dlp.YoutubeDL
_INFO = json.loads(os.environ["SAVETUBE_E2E_INFO"])
_MEDIA = os.environ["SAVETUBE_E2E_MEDIA"]
_VIDEO = os.environ["SAVETUBE_E2E_VIDEO"]
_PLAYLIST = os.environ["SAVETUBE_E2E_PLAYLIST"]


class FakeYoutubeDL:
    sanitize_info = staticmethod(_Real.sanitize_info)

    def __init__(self, params=None, auto_init=True):
        self.params = params or {}
        self._before_dl = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, pp, when="post_process"):
        if when == "before_dl":
            self._before_dl.append(pp)

    def extract_info(self, url, download=True, **kwargs):
        if not download:
            return self._listing(url)
        video_id = url.rsplit("=", 1)[-1]
        home = Path(self.params["paths"]["home"])
        home.mkdir(parents=True, exist_ok=True)
        media = home / f"{video_id}.mkv"
        info = {
            **_INFO,
            "id": video_id,
            "ext": "mkv",
            "filepath": str(media),
            "requested_downloads": [{"filepath": str(media), "ext": "mkv"}],
        }
        for pp in self._before_dl:
            pp.run(info)
        shutil.copyfile(_MEDIA, media)
        (home / f"{video_id}.en.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n")
        (home / f"{video_id}.info.json").write_text(json.dumps(info))
        finished = {
            "status": "finished",
            "filename": str(media),
            "info_dict": {"format_id": _INFO["format_id"], "filesize": media.stat().st_size},
        }
        for hook in self.params.get("progress_hooks") or []:
            hook(finished)
        for hook in self.params.get("postprocessor_hooks") or []:
            hook({"status": "started", "postprocessor": "Merger"})
            hook({"status": "finished", "postprocessor": "Merger"})
        return info

    def _listing(self, url):
        owner = {k: _INFO[k] for k in ("channel_id", "channel", "uploader_id")}
        if url.endswith("/videos"):
            return {**owner, "entries": [{"id": _VIDEO, "title": _INFO["title"]}]}
        if url.endswith("/playlists"):
            playlist = f"https://www.youtube.com/playlist?list={_PLAYLIST}"
            return {**owner, "entries": [{"id": _PLAYLIST, "title": "E2E list", "url": playlist}]}
        raise DownloadError(f"ERROR: [youtube:tab] This channel does not have a {url} tab")


yt_dlp.YoutubeDL = FakeYoutubeDL
"""


def make_app():
    """The uvicorn factory: the real app with login skipped."""
    from savetube.web.app import create_app
    from savetube.web.auth import User, current_user

    app = create_app()
    app.dependency_overrides[current_user] = lambda: User(sub="e2e", name="E2E", email=None)
    return app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Proc:
    """A subprocess with its output in a file, read back without the DSN."""

    def __init__(self, name: str, args: list[str], env: dict[str, str], cwd: Path, secret: str):
        self.name = name
        self.log = cwd / f"{name}.log"
        self._secret = secret
        self._out = self.log.open("wb")
        self.proc = subprocess.Popen(args, env=env, cwd=cwd, stdout=self._out, stderr=self._out)

    def tail(self) -> str:
        text = self.log.read_text(errors="replace")[-4000:]
        return text.replace(self._secret, "<dsn>")

    def wait_http(self, url: str) -> None:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                pytest.fail(f"{self.name} exited {self.proc.returncode}:\n{self.tail()}")
            try:
                if httpx2.get(url, timeout=2).status_code == 200:
                    return
            except httpx2.HTTPError:
                pass
            time.sleep(0.2)
        pytest.fail(f"{self.name} not ready after {STARTUP_TIMEOUT_S}s:\n{self.tail()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._out.close()


@pytest.fixture(scope="module")
def dsn():
    admin = Settings(database_url=ADMIN_URL)
    engine = sa.create_engine(admin.sqlalchemy_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {TEST_DB}"))
    url = urlunsplit(urlsplit(ADMIN_URL)._replace(path=f"/{TEST_DB}"))
    migrate(Settings(database_url=url))
    yield url
    with engine.connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
    engine.dispose()


@pytest.fixture(scope="module")
def db(dsn):
    engine = sa.create_engine(Settings(database_url=dsn).sqlalchemy_url)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> Path:
    """A 2 s mkv with a video and an Opus audio stream, or plain bytes without ffmpeg."""
    path = tmp_path_factory.mktemp("media") / "source.mkv"
    if HAVE_FFMPEG:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440",
                "-t", "2", "-c:v", "mpeg4", "-c:a", "libopus", str(path),
            ],
            check=True,
        )  # fmt: skip
    else:
        path.write_bytes(secrets.token_bytes(64 * 1024))
    return path


@pytest.fixture(scope="module")
def archive_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("archive")


@pytest.fixture(scope="module")
def app_env(dsn, archive_root, media, tmp_path_factory) -> dict[str, str]:
    """The environment of both app processes. No inherited SAVETUBE_ variable,
    and a working directory without a `.env`, so only these settings apply."""
    fake = tmp_path_factory.mktemp("fake-ytdlp")
    (fake / "sitecustomize.py").write_text(FAKE_SITECUSTOMIZE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAVETUBE_")}
    env.update(
        PYTHONPATH=os.pathsep.join([str(fake), str(Path(__file__).parent)]),
        SAVETUBE_DATABASE_URL=dsn,
        SAVETUBE_ARCHIVE_ROOT=str(archive_root),
        SAVETUBE_SESSION_SECRET="e2e",
        SAVETUBE_OIDC_CLIENT_ID="e2e",
        SAVETUBE_OIDC_ISSUER="https://issuer.test",
        SAVETUBE_PUBLIC_URL="http://127.0.0.1",
        SAVETUBE_E2E_INFO=json.dumps(INFO),
        SAVETUBE_E2E_MEDIA=str(media),
        SAVETUBE_E2E_VIDEO=VIDEO_ID,
        SAVETUBE_E2E_PLAYLIST=PLAYLIST_ID,
    )
    return env


@pytest.fixture(scope="module")
def seeded(db) -> dict[str, int]:
    """What a poll of the channel's videos tab leaves: the channel, its source,
    the listed video and its queued archive job."""
    with Session(db) as s:
        s.add(Channel(id=CHANNEL_ID, name=INFO["channel"], dir_name=f"E2E Channel [{CHANNEL_ID}]"))
        s.flush()
        source = Source(
            url=f"{CHANNEL_URL}/videos",
            kind=SourceKind.TAB,
            channel_id=CHANNEL_ID,
            archiving=True,
            last_polled_at=datetime.now(UTC),
        )
        s.add(source)
        s.add(Video(id=VIDEO_ID, channel_id=CHANNEL_ID, type=VideoType.VIDEOS, title=TITLE))
        s.flush()
        s.add(VideoSource(video_id=VIDEO_ID, source_id=source.id))
        s.add(
            ListingEntry(source_id=source.id, video_id=VIDEO_ID, channel_id=CHANNEL_ID, title=TITLE)
        )
        job = Job(kind=JobKind.ARCHIVE, state=JobState.QUEUED, video_id=VIDEO_ID)
        s.add(job)
        s.commit()
        return {"source": source.id, "job": job.id}


@pytest.fixture(scope="module")
def server(app_env, tmp_path_factory, dsn):
    port = _free_port()
    proc = _Proc(
        "web",
        [
            sys.executable, "-m", "uvicorn", "test_e2e:make_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        app_env,
        tmp_path_factory.mktemp("web"),
        dsn,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        proc.wait_http(f"{base}/healthz")
        with httpx2.Client(base_url=base, timeout=30) as client:
            yield client
    finally:
        proc.stop()


@pytest.fixture(scope="module")
def downloaded(app_env, seeded, db, dsn, tmp_path_factory) -> Job:
    """Run the seeded archive job through `savetube worker`, queued the way the
    scheduler's dispatch queues it, and return the finished job row."""
    health = _free_port()
    env = {
        **app_env,
        "SAVETUBE_HEALTH_PORT": str(health),
        "SAVETUBE_METRICS_PORT": str(_free_port()),
    }
    cli = Path(sys.executable).parent / "savetube"
    proc = _Proc("worker", [str(cli), "worker"], env, tmp_path_factory.mktemp("worker"), dsn)
    try:
        proc.wait_http(f"http://127.0.0.1:{health}/healthz")
        queue = App(connector=SyncPsycopgConnector(conninfo=dsn))
        with queue.open():
            queue.configure_task("archive", queue="archive").defer(job_id=seeded["job"])
        deadline = time.monotonic() + JOB_TIMEOUT_S
        while True:
            with Session(db) as s:
                job = s.get(Job, seeded["job"])
            if job.state not in (JobState.QUEUED, JobState.RUNNING):
                break
            if proc.proc.poll() is not None:
                pytest.fail(f"worker exited {proc.proc.returncode}:\n{proc.tail()}")
            if time.monotonic() > deadline:
                pytest.fail(f"job still {job.state} after {JOB_TIMEOUT_S}s:\n{proc.tail()}")
            time.sleep(0.5)
        assert job.state is JobState.SUCCEEDED, f"{job.state}: {job.log_tail}\n{proc.tail()}"
        return job
    finally:
        proc.stop()


def _video(db) -> Video:
    with Session(db) as s:
        return s.get(Video, VIDEO_ID)


def test_the_worker_stores_the_download_in_the_archive(downloaded, db, archive_root, media):
    assert downloaded.attempts == 1
    assert downloaded.finished_at is not None
    video = _video(db)
    assert video.state is VideoState.COMPLETE
    assert video.dir_path == (f"E2E Channel [{CHANNEL_ID}]/videos/2026-03-15 {TITLE} [{VIDEO_ID}]")
    assert video.info["id"] == VIDEO_ID
    assert video.resume_format is None
    assert video.verified_checks == (["size", "probe"] if HAVE_FFMPEG else ["size"])
    directory = archive_root / video.dir_path
    stored = directory / f"{VIDEO_ID}.mkv"
    assert stored.read_bytes() == media.read_bytes()
    assert (directory / f"{VIDEO_ID}.en.vtt").is_file()
    assert (directory / f"{VIDEO_ID}.opus").is_file() is HAVE_FFMPEG
    assert video.size_bytes == sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
    assert not (archive_root / ".incoming" / VIDEO_ID).exists()


def test_the_api_serves_the_stored_video(server, downloaded, media):
    r = server.get(f"/api/videos/{VIDEO_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "complete" and body["info"]["id"] == VIDEO_ID
    for key in ("completed_at", "verified_at", "verified_checks", "size_bytes", "type"):
        assert key in body

    content = media.read_bytes()
    r = server.get(f"/api/videos/{VIDEO_ID}/media", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-9/{len(content)}"
    assert r.content == content[:10]
    assert r.headers["content-type"] == "video/x-matroska"
    r = server.head(f"/api/videos/{VIDEO_ID}/media")
    assert r.status_code == 200 and r.headers["content-length"] == str(len(content))

    r = server.get(f"/api/videos/{VIDEO_ID}/audio", headers={"Range": "bytes=0-3"})
    if HAVE_FFMPEG:
        assert r.status_code == 206 and r.content == b"OggS"
    else:
        assert r.status_code == 404

    r = server.get(f"/api/videos/{VIDEO_ID}/subtitles")
    assert r.status_code == 200 and r.json() == [{"lang": "en", "label": "English"}]
    r = server.get(f"/api/videos/{VIDEO_ID}/subtitles/en")
    assert r.status_code == 200 and r.text.startswith("WEBVTT")
    assert server.get(f"/api/videos/{VIDEO_ID}/subtitles/fr").status_code == 404


def test_a_video_that_is_no_live_has_no_format_watch(server, downloaded):
    r = server.get(f"/api/videos/{VIDEO_ID}/formats")
    assert r.status_code == 200 and r.json() is None
    r = server.post(
        f"/api/videos/{VIDEO_ID}/formats/pick", json={"format_id": "251"}, headers=WRITE
    )
    assert r.status_code == 404


def test_the_api_lists_the_job_video_and_stats(server, downloaded, seeded):
    r = server.get("/api/jobs", params={"kind": "archive", "state": ["succeeded", "failed"]})
    assert r.status_code == 200
    [job] = [j for j in r.json() if j["id"] == seeded["job"]]
    assert job["state"] == "succeeded" and job["video_id"] == VIDEO_ID
    assert job["title"] == TITLE and job["channel_name"] == INFO["channel"]
    for key in ("reason", "log_tail", "attempts", "progress", "deferred_until", "finished_at"):
        assert key in job

    r = server.get("/api/jobs/counts", params={"kind": "archive", "channel_id": CHANNEL_ID})
    assert r.status_code == 200
    counts = r.json()
    assert set(counts) == {s.value for s in JobState} and counts["succeeded"] == 1

    for query in (
        {"channel_id": CHANNEL_ID},
        {"channel_name": INFO["channel"]},
        {"listed_by": CHANNEL_ID},
        {"source_id": seeded["source"]},
        {"type": "videos", "state": "complete", "q": "first", "order": "oldest"},
        # INFO's mpeg4 video and Opus audio, without a height.
        {"vcodec": "other", "acodec": "opus", "premium": "false"},
    ):
        r = server.get("/api/videos", params={**query, "limit": 10, "offset": 0})
        assert r.status_code == 200, query
        page = r.json()
        assert page["total"] == 1 and [v["id"] for v in page["items"]] == [VIDEO_ID], query

    r = server.get("/api/stats/summary")
    assert r.status_code == 200
    summary = r.json()
    assert summary["video_count"] == 1 and summary["total_bytes"] > 0
    assert summary["by_type"][0]["type"] == "videos"
    assert summary["by_channel"][0]["channel_id"] == CHANNEL_ID
    assert "free_bytes" in summary and "duration_seconds" in summary

    r = server.get("/api/stats/daily", params={"days": 30})
    assert r.status_code == 200
    [day] = r.json()
    assert day["count"] == 1 and day["bytes"] > 0 and "day" in day

    r = server.get("/api/channels")
    assert r.status_code == 200
    [channel] = [c for c in r.json() if c["id"] == CHANNEL_ID]
    assert channel["tracked"] is True and channel["video_count"] == 1
    for key in ("handle", "dir_name", "bytes", "include_words", "exclude_words", "date_after",
                "format", "watch_lives", "members_all", "playlists_pass", "keep_all",
                "avatar_url", "memberships"):  # fmt: skip
        assert key in channel


def test_a_write_without_the_csrf_header_is_refused(server):
    assert server.put("/api/words", json={}).status_code == 403


def test_me(server):
    r = server.get("/api/me")
    assert r.status_code == 200 and r.json()["sub"] == "e2e"
    assert {"name", "email", "groups"} <= r.json().keys()


def test_global_words_round_trip(server):
    body = {"include_words": ["Upload"], "exclude_words": ["teaser"]}
    r = server.put("/api/words", json=body, headers=WRITE)
    assert r.status_code == 200 and set(r.json()) == {"include_words", "exclude_words"}
    assert server.get("/api/words").json() == r.json()
    empty = {"include_words": [], "exclude_words": []}
    assert server.put("/api/words", json=empty, headers=WRITE).json() == empty


def test_title_previews(server, seeded):
    r = server.post(
        f"/api/sources/{seeded['source']}/title-preview",
        json={"include_words": [], "exclude_words": ["first"]},
        headers=WRITE,
    )
    assert r.status_code == 200
    assert r.json() == {
        "total": 1,
        "kept": 0,
        "titles": [{"video_id": VIDEO_ID, "title": TITLE, "kept": False}],
    }
    r = server.post(
        "/api/title-preview",
        json={"titles": ["a live", "an upload"], "exclude_words": ["live"], "channel_id": "UCx"},
        headers=WRITE,
    )
    assert r.status_code == 200
    assert r.json() == [{"title": "a live", "kept": False}, {"title": "an upload", "kept": True}]

    r = server.get(f"/api/channels/{CHANNEL_ID}/words")
    assert r.status_code == 200
    assert all({"word", "count"} <= w.keys() for w in r.json())


def test_a_playlist_source_lifecycle(server):
    url = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
    r = server.post("/api/sources", json={"url": url, "poll_interval_minutes": 60}, headers=WRITE)
    assert r.status_code == 201
    [source] = r.json()
    assert source["kind"] == "playlist" and source["url"] == url
    for key in ("channel_id", "parent_id", "title", "archiving", "include_words", "exclude_words",
                "other_channels", "enabled", "poll_interval_minutes", "last_polled_at",
                "last_error"):  # fmt: skip
        assert key in source
    assert server.post("/api/sources", json={"url": url}, headers=WRITE).status_code == 409

    r = server.patch(
        f"/api/sources/{source['id']}",
        json={"enabled": False, "archiving": True, "include_words": [" Live ", "live"]},
        headers=WRITE,
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["include_words"] == ["Live"]

    r = server.post(f"/api/sources/{source['id']}/poll", headers=WRITE)
    assert r.status_code == 202 and r.json()["last_polled_at"] is None

    assert any(s["id"] == source["id"] for s in server.get("/api/sources").json())
    r = server.delete(f"/api/sources/{source['id']}", headers=WRITE)
    assert r.status_code == 204
    assert all(s["id"] != source["id"] for s in server.get("/api/sources").json())


def test_a_channel_scan_then_add(server):
    r = server.post("/api/channel-scan", json={"url": f"{CHANNEL_URL}/videos"}, headers=WRITE)
    assert r.status_code == 200
    scan = r.json()
    assert scan["url"] == CHANNEL_URL and scan["channel_id"] == CHANNEL_ID
    assert scan["tabs"] == {
        "videos": [{"video_id": VIDEO_ID, "title": TITLE}],
        "shorts": None,
        "streams": None,
    }
    playlist_url = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
    assert scan["playlists"] == [{"id": PLAYLIST_ID, "title": "E2E list", "url": playlist_url}]
    assert {"handle", "name", "avatar_url", "suggestions"} <= scan.keys()

    r = server.post("/api/channel-playlists", json={"url": CHANNEL_URL}, headers=WRITE)
    assert r.status_code == 200
    assert r.json() == [
        {"id": PLAYLIST_ID, "title": "E2E list", "url": playlist_url, "source_id": None}
    ]
    bad = {"url": "https://example.com/x"}
    assert server.post("/api/channel-scan", json=bad, headers=WRITE).status_code == 422

    new_id = "UCe2eChannel000000000002"
    body = {
        "url": f"https://www.youtube.com/channel/{new_id}",
        "channel_id": new_id,
        "handle": "@e2e2",
        "name": "E2E Second",
        "avatar_url": None,
        "tabs": ["videos", "streams"],
        "all_playlists": False,
        "playlists": [{"url": playlist_url, "title": "E2E list"}],
        "other_channels": True,
        "include_words": [],
        "exclude_words": [],
        "date_after": None,
        "format": "bv*+ba/b",
        "watch_lives": True,
        "playlists_pass": True,
        "keep_all": True,
    }
    r = server.post("/api/channels", json=body, headers=WRITE)
    assert r.status_code == 201
    created = r.json()
    assert created["id"] == new_id and created["tracked"] is True
    assert created["playlists_pass"] is True and created["keep_all"] is True
    assert sorted(s["kind"] for s in created["sources"]) == ["playlist", "tab", "tab"]
    assert all(s["archiving"] for s in created["sources"])

    r = server.post("/api/channel-playlists", json={"url": CHANNEL_URL}, headers=WRITE)
    [listed] = r.json()
    assert listed["source_id"] is not None

    r = server.patch(
        f"/api/channels/{new_id}",
        json={
            "date_after": "2026-01-01",
            "exclude_words": [" Shorts", "shorts"],
            "watch_lives": False,
            "members_all": True,
            "playlists_pass": False,
            "keep_all": False,
        },
        headers=WRITE,
    )
    assert r.status_code == 200
    patched = r.json()
    assert patched["members_all"] is True and patched["playlists_pass"] is False
    assert patched["keep_all"] is False
    assert patched["date_after"] == "2026-01-01" and patched["exclude_words"] == ["Shorts"]
    assert patched["watch_lives"] is False
    assert server.patch("/api/channels/UCnone", json={}, headers=WRITE).status_code == 404


def test_download_settings_round_trip(server):
    r = server.get("/api/download-settings")
    assert r.status_code == 200
    for key in ("max_concurrent", "rate_limit_bytes", "window_start", "window_end",
                "window_enabled", "daily_limit", "cookie_profile", "timezone", "paused",
                "downloaded_today"):  # fmt: skip
        assert key in r.json()
    body = {
        "max_concurrent": 2,
        "rate_limit_bytes": 1_000_000,
        "window_start": "01:00",
        "window_end": "07:00",
        "window_enabled": False,
        "daily_limit": 20,
        "cookie_profile": None,
    }
    r = server.put("/api/download-settings", json=body, headers=WRITE)
    assert r.status_code == 200
    assert {k: r.json()[k] for k in body} == body
    unknown = {**body, "cookie_profile": "nobody"}
    assert server.put("/api/download-settings", json=unknown, headers=WRITE).status_code == 422

    r = server.put("/api/download-settings/paused", json={"paused": True}, headers=WRITE)
    assert r.status_code == 200 and r.json()["paused"] is True
    r = server.put("/api/download-settings/paused", json={"paused": False}, headers=WRITE)
    assert r.status_code == 200 and r.json()["paused"] is False

    r = server.get("/api/cookie-profiles")
    assert r.status_code == 200 and r.json() == []


def test_staging_move_request(server):
    r = server.get("/api/staging")
    assert r.status_code == 200
    assert r.json() == {"count": 0, "bytes": 0, "move_requested": False}
    r = server.post("/api/staging/move", headers=WRITE)
    assert r.status_code == 200 and r.json()["move_requested"] is True


def test_pods_are_unavailable_outside_a_cluster(server):
    r = server.get("/api/pods")
    assert r.status_code == 200
    assert r.json() == {"available": False, "recorders": []}


def test_every_api_route_is_covered():
    """A new route fails here until a test above calls it."""
    from savetube.web.app import create_app

    app = create_app(
        Settings(
            database_url="postgres://x@127.0.0.1/x",
            session_secret="s",
            oidc_client_id="c",
        )
    )
    # app.routes holds included routers, not their routes, so read the OpenAPI paths.
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/")
        for method in operations
        if method != "head"
    }
    assert len(routes) > 30
    source = Path(__file__).read_text()
    called = set(
        re.findall(r'server\.(get|post|put|patch|delete|head)\(\s*f?"(/api/[^"]*)"', source)
    )
    called = {(m.upper(), re.sub(r"\{[^}]*\}", "x", p)) for m, p in called}

    def hit(method, path):
        pattern = re.compile(re.sub(r"\\\{[^}]*\\\}", "[^/]+", re.escape(path)) + r"(\?.*)?")
        return any(m == method and pattern.fullmatch(p) for m, p in called)

    missing = sorted((m, p) for m, p in routes if not hit(m, p))
    assert missing == []
