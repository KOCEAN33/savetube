import pytest

from savetube.storage import finalize
from savetube.ytdl import WAIT_FOR_VIDEO, build_params, copy_cookies


def test_archive_params_sleep_and_never_record_from_start(tmp_path):
    p = build_params(tmp_path)
    assert p["format"] == "bv*+ba/b"
    assert p["merge_output_format"] == "mkv"
    assert p["outtmpl"] == {"default": "%(id)s.%(ext)s"}
    assert p["paths"] == {"home": str(tmp_path), "temp": str(tmp_path / ".tmp")}
    assert p["sleep_interval"] == 10 and p["max_sleep_interval"] == 20
    assert "live_from_start" not in p
    assert "cookiefile" not in p


def test_live_params_record_from_start_without_pre_download_sleep(tmp_path):
    p = build_params(tmp_path, live=True)
    assert p["live_from_start"] is True
    assert p["wait_for_video"] == WAIT_FOR_VIDEO
    assert "sleep_interval" not in p and "max_sleep_interval" not in p
    assert p["sleep_interval_requests"] == 0.75
    assert "format_sort" not in p


def _requested_subtitles(params):
    from yt_dlp import YoutubeDL

    subs = {
        "ja": [{"ext": "vtt", "url": "https://example.test/ja.vtt"}],
        "live_chat": [{"ext": "json", "url": "https://example.test/chat", "protocol": "x"}],
    }
    with YoutubeDL({**params, "quiet": True}) as ydl:
        return set(ydl.process_subtitles("v1", subs, {}) or {})


def test_a_recording_skips_the_live_chat_an_archive_keeps(tmp_path):
    """yt-dlp writes subtitles before the media, and the chat of a running live
    downloads until the stream ends."""
    assert _requested_subtitles(build_params(tmp_path, live=True)) == {"ja"}
    assert _requested_subtitles(build_params(tmp_path)) == {"ja", "live_chat"}


def test_a_dropped_connection_resumes_inside_one_download(tmp_path):
    """yt-dlp as a library retries nothing unless `retries` is set, so one
    dropped connection ended the whole run."""
    import http.server
    import threading

    from yt_dlp import YoutubeDL
    from yt_dlp.downloader.http import HttpFD

    body = bytes(range(256)) * 4096
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            start = int((self.headers.get("Range") or "bytes=0-").split("=")[1].split("-")[0])
            requests.append(start)
            rest = body[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(rest)))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            # The first response stops halfway, as googlevideo did.
            self.wfile.write(rest if start else rest[: len(rest) // 2])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        params = build_params(tmp_path) | {"quiet": True, "noprogress": True}
        target = tmp_path / "media.bin"
        with YoutubeDL(params) as ydl:
            ok = HttpFD(ydl, params).download(
                str(target), {"url": f"http://127.0.0.1:{server.server_port}/media"}
            )
    finally:
        server.shutdown()
    assert ok and target.read_bytes() == body
    # The retry continues near the cut, from the last block yt-dlp wrote.
    assert len(requests) == 2 and 0 < requests[1] <= len(body) // 2


def test_cookie_copy_is_writable_and_passed(tmp_path):
    src = tmp_path / "secret.txt"
    src.write_text("# Netscape HTTP Cookie File\n")
    src.chmod(0o400)
    work = tmp_path / "work"
    work.mkdir()
    copied = copy_cookies(src, work)
    assert copied.read_text() == "# Netscape HTTP Cookie File\n"
    copied.write_text("rewritten")  # yt-dlp writes the jar back
    assert build_params(work, cookiefile=copied)["cookiefile"] == str(copied)


def test_finalize_moves_and_refuses_to_overwrite(tmp_path):
    incoming = tmp_path / ".incoming" / "7"
    (incoming / ".tmp").mkdir(parents=True)
    (incoming / "vid.mkv").write_bytes(b"x")
    final = tmp_path / "C [UC1]" / "videos" / "2026-01-01 T [vid]"
    finalize(incoming, final)
    assert (final / "vid.mkv").read_bytes() == b"x"
    assert not (final / ".tmp").exists()
    assert not incoming.exists()

    other = tmp_path / ".incoming" / "8"
    other.mkdir()
    with pytest.raises(FileExistsError):
        finalize(other, final)
    assert other.exists()


# A 1080p60 video, its 720p60 and 480p siblings, two audio formats and one
# muxed 360p format, sorted worst to best as yt-dlp's extractor leaves them.
_FORMATS = [
    {"format_id": "18", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "height": 360},
    {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 129},
    {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135},
    {"format_id": "135", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 480},
    {"format_id": "298", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720},
    {"format_id": "299", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 1080},
]


def _chosen(selector):
    import yt_dlp

    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        pick = ydl.build_format_selector(selector)
        formats = [{**f, "url": f"https://example.com/{f['format_id']}"} for f in _FORMATS]
        ctx = {"formats": formats, "has_merged_format": True, "incomplete_formats": False}
        return [f["format_id"] for f in pick(ctx)]


@pytest.mark.parametrize(
    ("selector", "excluded", "chosen"),
    [
        ("bv*+ba/b", [], ["299+251"]),
        ("bv*+ba/b", ["299"], ["298+251"]),
        ("bv*+ba/b", ["299", "298"], ["135+251"]),
        ("bv*+ba/b", ["251"], ["299+140"]),
        ("bv*[height<=1080]+ba/b[height<=1080]", ["299"], ["298+251"]),
        ("ba/b", ["251"], ["140"]),
        ("ba/b", ["251", "140"], ["18"]),
        (" bv*+ba/b ", ["299"], ["298+251"]),
    ],
)
def test_excluding_formats_drops_them_from_every_alternative(selector, excluded, chosen):
    from savetube.ytdl import exclude_formats

    assert _chosen(exclude_formats(selector, excluded)) == chosen


def test_an_excluded_id_with_a_quote_still_parses():
    from savetube.ytdl import exclude_formats

    assert _chosen(exclude_formats("bv*+ba/b", ["it's"])) == ["299+251"]


@pytest.mark.parametrize(
    ("message", "server"),
    [
        (
            "DownloadError: ERROR: \r[download] Got error: HTTP Error 500: Internal Server Error."
            " Giving up after 10 retries",
            True,
        ),
        ("ERROR: \r[download] Got error: HTTP Error 500: Internal Server Error", False),
        (
            "ERROR: [download] Got error: HTTP Error 503: Service Unavailable."
            " Giving up after 10 retries",
            False,
        ),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", False),
        # HLS: every fragment got HTTP 500 and yt-dlp skipped each one.
        ("DownloadError: ERROR: The downloaded file is empty", True),
    ],
)
def test_a_format_server_error_is_http_500_after_every_retry(message, server):
    from savetube.ytdl import format_server_error

    assert format_server_error(message) is server


@pytest.mark.parametrize(
    ("message", "bare"),
    [
        ("DownloadError: ERROR: [youtube] a-b_1: Video unavailable", True),
        ("DownloadError: ERROR: [youtube] a-b_1: Video unavailable. ", True),
        ("ERROR: [youtube] a-b_1: Video unavailable\n", True),
        (
            "DownloadError: ERROR: [youtube] a-b_1: Video unavailable."
            " This video has been removed by the uploader",
            False,
        ),
        (
            "DownloadError: ERROR: [youtube] a-b_1: Video unavailable. This video is no longer"
            " available because the YouTube account associated with this video has been"
            " terminated.",
            False,
        ),
        ("DownloadError: ERROR: [youtube] a-b_1: Private video", False),
        ("DownloadError: ERROR: [youtube] a-b_1: This video is unavailable", False),
    ],
)
def test_a_bare_unavailable_has_no_sub_reason(message, bare):
    from savetube.ytdl import bare_unavailable

    assert bare_unavailable(message) is bare


# The 1080p30 formats of 1BXpG8kzeqA as yt-dlp 2026.08.19 lists them, with the
# extractor's sort fields. Premium VP9 616 has `source_preference` 99, so
# yt-dlp's own order picks it over AV1 399.
_YOUTUBE_SORT = ["quality", "res", "fps", "hdr:12", "source", "vcodec", "channels", "acodec"]
_1080P = [
    {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "quality": 3, "abr": 129},
    {"format_id": "251-drc", "vcodec": "none", "acodec": "opus", "quality": 2.5, "abr": 132},
    {"format_id": "251", "vcodec": "none", "acodec": "opus", "quality": 3, "abr": 132},
    *(
        {"format_id": i, "vcodec": c, "acodec": "none", "height": 1080, "width": 1920, "fps": 30}
        | {"quality": 9, "source_preference": s, "tbr": t}
        for i, c, s, t in [
            ("137", "avc1.640028", -1, 496),
            ("248", "vp9", -1, 1465),
            ("399", "av01.0.08M.08", -1, 632),
            ("616", "vp09.00.40.08", 99, 4306),
        ]
    ),
]
_1440P_VP9 = {
    "format_id": "271",
    "vcodec": "vp9",
    "acodec": "none",
    "height": 1440,
    "width": 2560,
    "fps": 30,
    "quality": 10,
    "source_preference": -1,
    "tbr": 3000,
}


def _download_pick(params, formats):
    from yt_dlp import YoutubeDL

    info = {
        "id": "v1",
        "title": "t",
        "extractor": "youtube",
        "extractor_key": "Youtube",
        "webpage_url": "https://www.youtube.com/watch?v=v1",
        "_format_sort_fields": _YOUTUBE_SORT,
        "formats": [
            {**f, "ext": "webm", "url": f"https://example.test/{f['format_id']}"} for f in formats
        ],
    }
    with YoutubeDL({**params, "quiet": True, "simulate": True}) as ydl:
        return ydl.process_ie_result(info, download=False)["format_id"]


def test_a_download_prefers_av1_over_premium_vp9_at_the_same_resolution(tmp_path):
    assert _download_pick(build_params(tmp_path), _1080P) == "399+251"


def test_a_download_keeps_a_higher_resolution_over_av1(tmp_path):
    assert _download_pick(build_params(tmp_path), [*_1080P, _1440P_VP9]) == "271+251"


_2160P60 = [
    {
        "format_id": i,
        "vcodec": c,
        "acodec": "none",
        "height": 2160,
        "width": 3840,
        "fps": 60,
        "quality": 12,
        "source_preference": -1,
        "tbr": t,
        "dynamic_range": r,
    }
    for i, c, r, t in [
        ("337", "vp09.02.51.10.01.09.16.09.0", "HDR10", 20000),
        ("401", "av01.0.12M.08", "SDR", 12000),
    ]
]
_AUDIO = {"format_id": "251", "vcodec": "none", "acodec": "opus", "quality": 3, "abr": 132}


def test_a_download_prefers_hdr_over_av1_at_the_same_resolution_and_fps(tmp_path):
    assert _download_pick(build_params(tmp_path), [_AUDIO, *_2160P60]) == "337+251"


def test_a_download_prefers_av1_between_two_sdr_formats(tmp_path):
    vp9_sdr = {**_2160P60[0], "format_id": "313", "dynamic_range": "SDR"}
    assert _download_pick(build_params(tmp_path), [_AUDIO, vp9_sdr, _2160P60[1]]) == "401+251"
