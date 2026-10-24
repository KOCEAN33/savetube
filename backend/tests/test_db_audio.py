import os

import pytest
from conftest import (
    ADMIN_URL,
    AUDIO_INFO,
    _events,
    _FakeTools,
    _fields,
    _finished,
    _home,
    _job_rows,
    _run_steps,
    _sql,
    _sql_rows,
    _Steps,
)

from savetube.db import make_engine, make_sessionmaker
from savetube.db.models import (
    JobState,
    VideoState,
)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


def _fake_bin(monkeypatch, tmp_path, *, probe=None, ffmpeg_error=None):
    """ffprobe printing `probe` as JSON, and ffmpeg that writes half a file to
    its output and fails with `ffmpeg_error`, first on PATH."""
    import json

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if probe is not None:
        (bin_dir / "ffprobe").write_text(f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(probe)}\nEOF\n")
    if ffmpeg_error is not None:
        (bin_dir / "ffmpeg").write_text(
            '#!/bin/bash\nfor a; do last="$a"; done\nprintf half > "$last"\n'
            f"echo '{ffmpeg_error}' >&2\nexit 1\n"
        )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


MERGED_PROBE = {
    "format": {"duration": "60.0"},
    "streams": [{"codec_type": "video"}, {"codec_type": "audio", "codec_name": "opus"}],
}


@pytest.mark.anyio
async def test_an_archived_video_gets_its_audio_copy_beside_it_in_the_tree(settings):
    from test_audio import make_media, needs_ffmpeg

    from savetube.integrity import Tools

    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg or ffprobe not on PATH")
    _job_rows(settings)

    async def merged(params, report):
        home = _home(params)
        home.mkdir(parents=True, exist_ok=True)
        media = make_media(home / "v1.mkv")
        return {
            **AUDIO_INFO,
            "duration": 1,
            "vcodec": "mp4v",
            "ext": "mkv",
            "requested_downloads": [{"filepath": str(media)}],
        }

    jobs, video = await _run_steps(settings, _Steps(merged), Tools())
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    d = settings.archive_root / video.dir_path
    assert sorted(p.name for p in d.iterdir()) == ["v1.mkv", "v1.opus"]
    # size_bytes counts every file in the directory, the audio copy too.
    assert video.size_bytes == (d / "v1.mkv").stat().st_size + (d / "v1.opus").stat().st_size


@pytest.mark.anyio
async def test_a_failed_audio_copy_logs_a_warning_and_the_video_stays_complete(
    settings, caplog, monkeypatch, tmp_path
):
    import logging

    _job_rows(settings, "UPDATE channel SET format = 'wa'")
    _fake_bin(monkeypatch, tmp_path, ffmpeg_error="Invalid data found when processing input")

    class Merged(_FakeTools):
        def probe(self, path):
            return MERGED_PROBE

    with caplog.at_level(logging.INFO, logger="savetube"):
        jobs, video = await _run_steps(settings, _Steps(_finished()), Merged())
    assert jobs[1].state is JobState.SUCCEEDED and video.state is VideoState.COMPLETE
    [failed] = _events(caplog, "audio.failed")
    assert failed.levelno == logging.WARNING
    assert "Invalid data found" in _fields(failed)["reason"]
    d = settings.archive_root / video.dir_path
    assert sorted(p.name for p in d.iterdir()) == ["v1.webm"]


def _stored_videos(settings, media=None):
    """a1 lacks its audio copy, b2 has one, c3 lost its video file, d4 is not
    stored, e5 is a live recording."""
    root = settings.archive_root / "A [UC1]" / "videos"
    for vid, files in [
        ("a1", ["a1.mkv", "a1.info.json"]),
        ("b2", ["b2.mkv", "b2.m4a"]),
        ("c3", ["c3.info.json"]),
        ("e5", ["e5.mkv"]),
    ]:
        (root / vid).mkdir(parents=True)
        for name in files:
            (root / vid / name).write_bytes(b"x" * 10)
    if media is not None:
        media(root / "a1" / "a1.mkv")
    rows = ", ".join(
        f"('{vid}', 'UC1', 'videos', 't', '{state}', 'A [UC1]/videos/{vid}', 20,"
        """ '{"ext": "mkv"}')"""
        for vid, state in [
            ("a1", "complete"),
            ("b2", "complete"),
            ("c3", "complete"),
            ("d4", "pending"),
            ("e5", "complete"),
        ]
    )
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, size_bytes, info)"
        f" VALUES {rows}",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES ('record', 'succeeded', 'e5', 1)",
    )
    return root


def _tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.anyio
async def test_the_audio_backfill_dry_run_lists_each_video_and_writes_nothing(
    settings, monkeypatch, tmp_path
):
    from savetube.audio import backfill
    from savetube.integrity import Tools

    root = _stored_videos(settings)
    before = _tree(root)
    _fake_bin(monkeypatch, tmp_path, probe=MERGED_PROBE)
    lines = []
    engine = make_engine(settings.sqlalchemy_url)
    counts = await backfill(
        make_sessionmaker(engine),
        settings.archive_root,
        tools=Tools(),
        dry_run=True,
        out=lines.append,
    )
    await engine.dispose()
    assert lines == [
        "a1 would extract A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "would extract 1, skipped 1, failed 1",
    ]
    assert counts == {"extracted": 1, "skipped": 1, "failed": 1}
    assert _tree(root) == before
    assert _sql_rows(settings, "SELECT DISTINCT size_bytes FROM video") == [(20,)]


def test_the_extract_audio_command_dry_run(settings, monkeypatch, tmp_path, capsys):
    from savetube import cli
    from savetube.config import get_settings

    root = _stored_videos(settings)
    before = _tree(root)
    _fake_bin(monkeypatch, tmp_path, probe=MERGED_PROBE)
    monkeypatch.setenv("SAVETUBE_DATABASE_URL", settings.database_url.get_secret_value())
    monkeypatch.setenv("SAVETUBE_ARCHIVE_ROOT", str(settings.archive_root))
    monkeypatch.setattr("sys.argv", ["savetube", "extract-audio", "--dry-run"])
    # The command's logging setup would replace pytest's handlers.
    monkeypatch.setattr("savetube.logs.configure", lambda level: None)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as e:
            cli.main()
    finally:
        get_settings.cache_clear()
    assert e.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        "a1 would extract A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "would extract 1, skipped 1, failed 1",
    ]
    assert _tree(root) == before


@pytest.mark.anyio
async def test_the_audio_backfill_writes_missing_copies_once(settings):
    from test_audio import make_media, needs_ffmpeg

    from savetube.audio import backfill
    from savetube.integrity import Tools

    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg or ffprobe not on PATH")
    root = _stored_videos(settings, media=make_media)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    first, second = [], []
    await backfill(sessionmaker, settings.archive_root, tools=Tools(), out=first.append)
    await backfill(sessionmaker, settings.archive_root, tools=Tools(), out=second.append)
    await engine.dispose()
    assert first == [
        "a1 extracted A [UC1]/videos/a1/a1.opus",
        "b2 skipped: has b2.m4a",
        "c3 failed: video file not found",
        "extracted 1, skipped 1, failed 1",
    ]
    assert second[0] == "a1 skipped: has a1.opus"
    assert second[-1] == "extracted 0, skipped 2, failed 1"
    a1 = root / "a1"
    assert sorted(p.name for p in a1.iterdir()) == ["a1.info.json", "a1.mkv", "a1.opus"]
    [(size,)] = _sql_rows(settings, "SELECT size_bytes FROM video WHERE id = 'a1'")
    assert size == sum(p.stat().st_size for p in a1.iterdir())


def _stored_recordings(settings, make_media):
    """r1 a recording to verify, r2 one a replace verified, r3 an archive
    download, r4 a recording that fails its checks."""
    root = settings.archive_root / "A [UC1]" / "lives"
    for vid in ("r1", "r2", "r3", "r4"):
        (root / vid).mkdir(parents=True)
    make_media(root / "r1" / "r1.mkv")
    make_media(root / "r2" / "r2.mkv")
    make_media(root / "r3" / "r3.mkv")
    (root / "r4" / "r4.mkv").write_bytes(b"recording")
    rows = ", ".join(
        f"('{vid}', 'UC1', 'lives', 't', 'complete', 'A [UC1]/lives/{vid}', 20,"
        f""" '{{"ext": "mkv", "vcodec": "mpeg4", "acodec": "opus"}}', {verified})"""
        for vid, verified in [
            ("r1", "NULL"),
            ("r2", "'2026-10-01T00:00:00Z'"),
            ("r3", "NULL"),
            ("r4", "NULL"),
        ]
    )
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'A', 'A [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, size_bytes, info,"
        f" verified_at) VALUES {rows}",
        "INSERT INTO job (kind, state, video_id, attempts) VALUES"
        " ('record', 'succeeded', 'r1', 1), ('record', 'succeeded', 'r2', 1),"
        " ('archive', 'succeeded', 'r3', 1), ('record', 'succeeded', 'r4', 1)",
    )
    return root


@pytest.mark.anyio
async def test_the_recording_backfill_verifies_each_unverified_recording_once(settings):
    from test_audio import make_media, needs_ffmpeg

    from savetube.audio import verify_recordings
    from savetube.integrity import Tools

    if needs_ffmpeg.args[0]:
        pytest.skip("ffmpeg or ffprobe not on PATH")
    root = _stored_recordings(settings, make_media)
    engine = make_engine(settings.sqlalchemy_url)
    sessionmaker = make_sessionmaker(engine)
    dry, first, second = [], [], []
    run = verify_recordings
    await run(sessionmaker, settings.archive_root, tools=Tools(), dry_run=True, out=dry.append)
    unchanged = _sql_rows(settings, "SELECT id FROM video WHERE verified_at IS NULL ORDER BY id")
    await run(sessionmaker, settings.archive_root, tools=Tools(), out=first.append)
    await run(sessionmaker, settings.archive_root, tools=Tools(), out=second.append)
    await engine.dispose()

    assert dry == [
        "r1 would verify A [UC1]/lives/r1/r1.mkv",
        "r4 would verify A [UC1]/lives/r4/r4.mkv",
        "would verify 2, failed 0",
    ]
    assert unchanged == [("r1",), ("r3",), ("r4",)]
    assert first[0] == "r1 verified probe,packets"
    assert first[1].startswith("r4 failed: probe ")
    assert first[2] == "verified 1, failed 1"
    # r1 is done. r4 stays unverified, so each run tries it again.
    assert [line.split(":")[0] for line in second] == ["r4 failed", "verified 0, failed 1"]
    assert sorted(p.name for p in (root / "r1").iterdir()) == ["r1.mkv", "r1.opus"]
    assert sorted(p.name for p in (root / "r2").iterdir()) == ["r2.mkv"]
    rows = _sql_rows(settings, "SELECT id, verified_checks, size_bytes FROM video ORDER BY id")
    r1_size = sum(p.stat().st_size for p in (root / "r1").iterdir())
    assert rows == [
        ("r1", ["probe", "packets"], r1_size),
        ("r2", None, 20),
        ("r3", None, 20),
        ("r4", None, 20),
    ]
