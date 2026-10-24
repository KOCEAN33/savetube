"""The Videos page's quality filters and columns on GET /api/videos."""

import json

import pytest
from conftest import ADMIN_URL, _api, _sql

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="SAVETUBE_TEST_DATABASE_URL unset")


def _merged(video: dict, audio: dict) -> dict:
    """An info dict as yt-dlp returns it for `<video>+<audio>`."""
    return {
        "format_id": f"{video['format_id']}+{audio['format_id']}",
        "vcodec": video["vcodec"],
        "acodec": audio["acodec"],
        "height": video.get("height"),
        "format_note": f"{video.get('format_note')}+{audio.get('format_note')}",
        "requested_formats": [video, audio],
    }


OPUS = {"format_id": "251", "vcodec": "none", "acodec": "opus", "abr": 130.0, "vbr": 0}
AAC = {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129.0}
INFOS = {
    "av1": _merged(
        {
            "format_id": "399",
            "vcodec": "av01.0.09M.08",
            "acodec": "none",
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "vbr": 2500.5,
            "tbr": 2500.5,
            "format_note": "1080p60",
        },
        OPUS,
    ),
    "premium": _merged(
        {
            "format_id": "616",
            "vcodec": "vp09.00.40.08",
            "acodec": "none",
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "vbr": 5500.0,
            "format_note": "1080p Premium",
        },
        OPUS,
    ),
    "short": _merged(
        {
            "format_id": "248",
            "vcodec": "vp9",
            "acodec": "none",
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "tbr": 2700.0,
            "format_note": "1080p",
        },
        OPUS,
    ),
    "h264": _merged(
        {
            "format_id": "136",
            "vcodec": "avc1.4d401f",
            "acodec": "none",
            "width": 1280,
            "height": 720,
            "fps": 30,
            "vbr": 1500.0,
            "format_note": "720p",
        },
        AAC,
    ),
    "uhd": _merged(
        {
            "format_id": "401",
            "vcodec": "av01.0.13M.08",
            "acodec": "none",
            "width": 3840,
            "height": 2160,
            "fps": 60,
            "vbr": 9000.0,
            "format_note": "2160p60",
        },
        OPUS,
    ),
    # One muxed format, no requested_formats.
    "muxed": {
        "format_id": "18",
        "vcodec": "mp4v.20.3",
        "acodec": "mp4a.40.2",
        "width": 640,
        "height": 360,
        "fps": 30,
        "tbr": 500.0,
        "format_note": "360p",
    },
    # A replace swapped the stored video stream in requested_formats, and the
    # top-level note still names the old Premium stream.
    "replaced": {
        **_merged(
            {
                "format_id": "248",
                "vcodec": "vp9",
                "acodec": "none",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vbr": 2800.0,
                "format_note": "1080p",
            },
            OPUS,
        ),
        "format_note": "1080p Premium+medium",
    },
}


def _rows(settings):
    statements = ["INSERT INTO channel (id, name, dir_name) VALUES ('UC1', 'Ch', 'Ch [UC1]')"]
    for vid, info in INFOS.items():
        statements.append(
            "INSERT INTO video (id, channel_id, type, title, state, format_id, info) VALUES"
            f" ('{vid}', 'UC1', 'videos', '{vid}', 'complete', '{info['format_id']}',"
            f" '{json.dumps(info)}')"
        )
    statements.append(
        "INSERT INTO video (id, channel_id, type, title, state)"
        " VALUES ('pending', 'UC1', 'videos', 'pending', 'pending')"
    )
    _sql(settings, *statements)


def _ids(client, **params) -> set[str]:
    r = client.get("/api/videos", params=params)
    assert r.status_code == 200, r.text
    page = r.json()
    ids = {v["id"] for v in page["items"]}
    assert page["total"] == len(ids)
    return ids


def test_filters_by_the_stored_video_codec(settings):
    _rows(settings)
    with _api(settings) as client:
        assert _ids(client, vcodec="av1") == {"av1", "uhd"}
        assert _ids(client, vcodec="vp9") == {"premium", "short", "replaced"}
        assert _ids(client, vcodec="h264") == {"h264"}
        # A known codec outside the three. A video without info has none.
        assert _ids(client, vcodec="other") == {"muxed"}


def test_filters_by_the_stored_audio_codec(settings):
    _rows(settings)
    with _api(settings) as client:
        assert _ids(client, acodec="aac") == {"h264", "muxed"}
        assert _ids(client, acodec="opus") == {"av1", "premium", "short", "uhd", "replaced"}


def test_filters_by_resolution_bucket_on_the_short_side(settings):
    _rows(settings)
    with _api(settings) as client:
        assert _ids(client, height="2160") == {"uhd"}
        assert _ids(client, height="1440") == set()
        # The 1080x1920 short counts as 1080.
        assert _ids(client, height="1080") == {"av1", "premium", "short", "replaced"}
        assert _ids(client, height="720") == {"h264"}
        assert _ids(client, height="below") == {"muxed"}


def test_premium_reads_the_stored_video_stream(settings):
    _rows(settings)
    with _api(settings) as client:
        assert _ids(client, premium="true") == {"premium"}
        assert "replaced" in _ids(client, premium="false")
        assert _ids(client, vcodec="vp9", height="1080", premium="false") == {"short", "replaced"}


def test_rows_carry_the_stored_stream_quality(settings):
    _rows(settings)
    with _api(settings) as client:
        items = {v["id"]: v for v in client.get("/api/videos").json()["items"]}
    quality = {
        vid: (
            v["format_id"],
            v["video_codec"],
            v["audio_codec"],
            v["video_width"],
            v["video_height"],
            v["video_fps"],
            v["video_kbps"],
            v["premium"],
        )
        for vid, v in items.items()
    }
    assert quality["av1"] == ("399+251", "AV1", "Opus", 1920, 1080, 60.0, 2500.5, False)
    assert quality["premium"] == ("616+251", "VP9", "Opus", 1920, 1080, 30.0, 5500.0, True)
    # Without vbr, the video stream's tbr.
    assert quality["short"] == ("248+251", "VP9", "Opus", 1080, 1920, 30.0, 2700.0, False)
    assert quality["h264"] == ("136+140", "H.264", "AAC", 1280, 720, 30.0, 1500.0, False)
    assert quality["muxed"] == ("18", "mp4v.20.3", "AAC", 640, 360, 30.0, 500.0, False)
    assert quality["pending"] == (None, None, None, None, None, None, None, False)


def test_a_bad_bucket_is_rejected(settings):
    with _api(settings) as client:
        assert client.get("/api/videos", params={"height": "480"}).status_code == 422
        assert client.get("/api/videos", params={"vcodec": "hevc"}).status_code == 422
