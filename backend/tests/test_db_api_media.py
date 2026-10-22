import json

import pytest
from conftest import ADMIN_URL, _api, _sql
from fastapi.testclient import TestClient

from savetube.web.app import create_app

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


MEDIA = bytes(range(256)) * 4


def _media_rows(settings, *, state="complete", dir_path="Ch [UC1]/videos/a [m1]"):
    root = settings.archive_root
    _sql(
        settings,
        "INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Ch', 'Ch [UC1]')",
        "INSERT INTO video (id, channel_id, type, title, state, dir_path, info) VALUES"
        f" ('m1', 'UC1', 'videos', 'a', '{state}', '{dir_path}', '{{\"ext\": \"mkv\"}}')",
    )
    directory = root / "Ch [UC1]/videos/a [m1]"
    directory.mkdir(parents=True)
    (directory / "m1.mkv").write_bytes(MEDIA)
    (directory / "m1.opus").write_bytes(MEDIA[:100])
    return directory


def test_media_serves_the_whole_file_with_matroska_type(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media")
    assert r.status_code == 200
    assert r.content == MEDIA
    assert r.headers["content-type"] == "video/x-matroska"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-length"] == "1024"


def test_media_serves_a_byte_range(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == MEDIA[10:20]
    assert r.headers["content-range"] == "bytes 10-19/1024"
    assert r.headers["content-type"] == "video/x-matroska"


def test_media_serves_a_suffix_range(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=-24"})
    assert r.status_code == 206
    assert r.content == MEDIA[1000:]
    assert r.headers["content-range"] == "bytes 1000-1023/1024"


def test_media_refuses_a_range_past_the_end(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/media", headers={"Range": "bytes=5000-6000"})
    assert r.status_code == 416
    assert r.headers["content-range"] == "bytes */1024"


def test_media_answers_head_without_a_body(settings):
    _media_rows(settings)
    with _api(settings) as client:
        r = client.head("/api/videos/m1/media")
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers["content-length"] == "1024"
    assert r.headers["content-type"] == "video/x-matroska"


def test_audio_serves_the_opus_copy_as_ogg(settings):
    _media_rows(settings)
    with _api(settings) as client:
        full = client.get("/api/videos/m1/audio")
        part = client.get("/api/videos/m1/audio", headers={"Range": "bytes=0-9"})
    assert full.status_code == 200 and full.content == MEDIA[:100]
    assert full.headers["content-type"] == "audio/ogg"
    assert part.status_code == 206 and part.content == MEDIA[:10]


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_media_is_404_until_the_video_is_complete(settings, state):
    _media_rows(settings, state=state)
    with _api(settings) as client:
        assert client.get("/api/videos/m1/media").status_code == 404
        assert client.get("/api/videos/m1/audio").status_code == 404


def test_media_is_404_for_a_missing_file_or_video(settings):
    directory = _media_rows(settings)
    (directory / "m1.opus").unlink()
    with _api(settings) as client:
        assert client.get("/api/videos/m1/audio").status_code == 404
        assert client.get("/api/videos/nope/media").status_code == 404


def test_media_never_leaves_the_archive_root(settings):
    outside = settings.archive_root.parent / "outside"
    outside.mkdir(parents=True)
    (outside / "m1.mkv").write_bytes(b"secret")
    _media_rows(settings, dir_path="../outside")
    with _api(settings) as client:
        assert client.get("/api/videos/m1/media").status_code == 404


def test_media_needs_a_login(settings):
    _media_rows(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.get("/api/videos/m1/media")
    assert r.status_code == 401


VTT_JA = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nこんにちは\n"


def _subtitle_rows(settings, *, state="complete", dir_path="Ch [UC1]/videos/a [m1]"):
    directory = _media_rows(settings, state=state, dir_path=dir_path)
    info = json.dumps({"ext": "mkv", "subtitles": {"ja": [{"ext": "vtt", "name": "Japanese"}]}})
    _sql(settings, f"UPDATE video SET info = '{info}' WHERE id = 'm1'")
    (directory / "m1.ja.vtt").write_text(VTT_JA, encoding="utf-8")
    (directory / "m1.zh-Hans.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (directory / "m1.live_chat.json").write_text("{}", encoding="utf-8")
    return directory


def test_subtitles_lists_stored_vtt_languages_with_labels(settings):
    directory = _subtitle_rows(settings)
    (directory / "other.en.vtt").write_text("WEBVTT\n", encoding="utf-8")
    with _api(settings) as client:
        r = client.get("/api/videos/m1/subtitles")
    assert r.status_code == 200
    assert r.json() == [
        {"lang": "ja", "label": "Japanese"},
        {"lang": "zh-Hans", "label": "zh-Hans"},
    ]


def test_subtitles_serves_the_vtt_file(settings):
    _subtitle_rows(settings)
    with _api(settings) as client:
        r = client.get("/api/videos/m1/subtitles/ja")
    assert r.status_code == 200
    assert r.content.decode("utf-8") == VTT_JA
    assert r.headers["content-type"] == "text/vtt; charset=utf-8"


@pytest.mark.parametrize(
    "lang", ["en", "live_chat", "JA", "..%2F..%2Fm1.ja", "ja.vtt", "%2E%2E", "ja%00"]
)
def test_subtitles_is_404_for_an_unlisted_language(settings, lang):
    _subtitle_rows(settings)
    with _api(settings) as client:
        assert client.get(f"/api/videos/m1/subtitles/{lang}").status_code == 404


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_subtitles_is_404_until_the_video_is_complete(settings, state):
    _subtitle_rows(settings, state=state)
    with _api(settings) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 404
        assert client.get("/api/videos/m1/subtitles/ja").status_code == 404


def test_subtitles_never_leave_the_archive_root(settings):
    outside = settings.archive_root.parent / "outside"
    outside.mkdir(parents=True)
    (outside / "m1.en.vtt").write_text("WEBVTT\n", encoding="utf-8")
    _subtitle_rows(settings, dir_path="../outside")
    with _api(settings) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 404
        assert client.get("/api/videos/m1/subtitles/en").status_code == 404


def test_subtitles_need_a_login(settings):
    _subtitle_rows(settings)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/videos/m1/subtitles").status_code == 401
        assert client.get("/api/videos/m1/subtitles/ja").status_code == 401
