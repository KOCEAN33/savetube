import pytest

from savetube.storage import finalize
from savetube.ytdl import WAIT_FOR_VIDEO, NotStarted, build_params, copy_cookies, download


def test_archive_params_never_record_from_start(tmp_path):
    p = build_params(tmp_path)
    assert p["format"] == "bv*+ba/b"
    assert p["merge_output_format"] == "mkv"
    assert p["paths"] == {"home": str(tmp_path), "temp": str(tmp_path / ".tmp")}
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


def test_a_recording_gives_up_on_a_stream_still_upcoming_at_its_deadline(tmp_path, monkeypatch):
    """yt-dlp's wait_for_video has no bound, so a cancelled waiting room kept
    its recorder waiting for good."""
    import threading
    import time

    import yt_dlp
    from yt_dlp.extractor.common import InfoExtractor

    extractions = []

    class _UpcomingIE(InfoExtractor):
        _VALID_URL = r"upcoming:(?P<id>\w+)"

        def _real_extract(self, url):
            extractions.append(time.monotonic())
            # As the YouTube extractor reports a waiting room past its start.
            self.raise_no_formats("This live event will begin in a few moments.", expected=True)
            return {
                "id": "v1",
                "title": "t",
                "live_status": "is_upcoming",
                "release_timestamp": int(time.time()) - 3600,
                "formats": [],
            }

    class _YoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, params):
            super().__init__(params, auto_init=False)
            self.add_info_extractor(_UpcomingIE())

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _YoutubeDL)
    params = build_params(tmp_path, live=True, wait_until=time.time() + 3)
    params["wait_for_video"] = (1, 1)
    outcome = []

    def run():
        started = time.monotonic()
        try:
            download("upcoming:v1", params)
        except Exception as e:
            outcome.append((type(e), time.monotonic() - started))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=15)
    assert not thread.is_alive(), "the recording still waits past its deadline"
    [(error, took)] = outcome
    assert error is NotStarted
    # It waited for the stream until the deadline, extracting again each second.
    assert took >= 3 and len(extractions) >= 3


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


FORBIDDEN = "DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden"


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        (FORBIDDEN, True),
        ("ERROR: unable to download video data: http error 403: forbidden", True),
        ("HTTP Error 429: Too Many Requests", False),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", False),
    ],
)
def test_a_forbidden_refusal_is_http_403(message, forbidden):
    from savetube.ytdl import forbidden as is_forbidden

    assert is_forbidden(message) is forbidden


@pytest.mark.parametrize(
    ("message", "permanent"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", True),
        ("Join this channel to get access to members-only content like this video", True),
        (FORBIDDEN, False),
        ("HTTP Error 429: Too Many Requests", False),
    ],
)
def test_a_permanent_failure_is_no_fault_of_the_session(message, permanent):
    from savetube.ytdl import permanent as is_permanent

    assert is_permanent(message) is permanent


UNREACHABLE = "http://127.0.0.1:9/x"


def _combined(format_id, height, url, **extra):
    return {
        "format_id": format_id,
        "url": url,
        "ext": "mp4",
        "height": height,
        "width": height * 16 // 9,
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        **extra,
    }


def _fake_youtube(monkeypatch, formats):
    """yt_dlp.YoutubeDL answers `fake:v1` with `formats`, nothing else."""
    import yt_dlp
    from yt_dlp.extractor.common import InfoExtractor

    class _FakeIE(InfoExtractor):
        _VALID_URL = r"fake:(?P<id>\w+)"

        def _real_extract(self, url):
            return {"id": "v1", "title": "t", "formats": [dict(f) for f in formats]}

    class _YoutubeDL(yt_dlp.YoutubeDL):
        def __init__(self, params):
            super().__init__(params, auto_init=False)
            self.add_info_extractor(_FakeIE())

    monkeypatch.setattr(yt_dlp, "YoutubeDL", _YoutubeDL)


class _Media:
    """A local HTTP server that records each path it serves."""

    def __init__(self):
        import http.server
        import threading

        self.paths = []
        paths = self.paths

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                if self.path.endswith(".m3u8"):
                    body = (
                        b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                        b"#EXTINF:1.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
                    )
                else:
                    body = b"\x00" * 4096
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


_NO_SLEEP = {"sleep_interval": 0, "max_sleep_interval": 0, "sleep_interval_requests": 0}


def _download_error(url, params):
    """Run `download`, return its error text, or None when it finished."""
    try:
        download(url, params | _NO_SLEEP | {"quiet": True, "noprogress": True})
    except BaseException as e:  # noqa: BLE001 - any failure is data here
        return f"{type(e).__name__}: {e}"
    return None


def test_an_archive_refuses_a_throttled_list_of_only_format_18(tmp_path, monkeypatch):
    """Throttling served members-only videos with only progressive 18 (360p),
    while a later probe offered 399 (1080p video-only) + 251."""
    from savetube.ytdl import DEGRADED_MARKER, degraded_formats

    _fake_youtube(monkeypatch, [_combined("18", 360, UNREACHABLE)])
    with pytest.raises(Exception) as caught:
        download("fake:v1", build_params(tmp_path) | {"quiet": True, "noprogress": True})
    text = str(caught.value)
    assert DEGRADED_MARKER in text
    assert degraded_formats(text)
    assert degraded_formats(f"DownloadError: ERROR: [youtube] v1: {text}")
    # Refused before any byte: no media file landed in the workdir.
    media = [
        p for p in tmp_path.rglob("*") if p.is_file() and not p.name.endswith((".json", ".txt"))
    ]
    assert media == []


def test_degraded_formats_ignores_other_failures():
    from savetube.ytdl import degraded_formats

    assert not degraded_formats(
        "DownloadError: ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
    assert not degraded_formats("DownloadError: ERROR: [youtube] v1: Video unavailable")


@pytest.mark.parametrize(
    ("formats", "live"),
    [
        # Uploaded at 360p: a video-only 360p format sits next to 18.
        (
            [
                {
                    "format_id": "134",
                    "path": "/134",
                    "ext": "mp4",
                    "height": 360,
                    "width": 640,
                    "vcodec": "avc1.4d401e",
                    "acodec": "none",
                },
                {**_combined("18", 360, ""), "path": "/18"},
            ],
            False,
        ),
        # A just-ended live offers only combined HLS formats, at 1080p.
        (
            [
                {
                    **_combined("96", 1080, ""),
                    "path": "/96.m3u8",
                    "protocol": "m3u8_native",
                    "ext": "mp4",
                }
            ],
            False,
        ),
        # A recording takes what the stream offers.
        ([{**_combined("18", 360, ""), "path": "/18"}], True),
    ],
    ids=["uploaded-at-360p", "combined-hls-1080p", "recording"],
)
def test_download_takes_a_list_that_is_not_degraded(tmp_path, monkeypatch, formats, live):
    media = _Media()
    try:
        served = []
        for f in formats:
            f = dict(f)
            f["url"] = media.base + f.pop("path")
            served.append(f)
        _fake_youtube(monkeypatch, served)
        error = _download_error("fake:v1", build_params(tmp_path, live=live))
    finally:
        media.close()
    assert error is None
    # The media download ran: a refusal comes before any request.
    assert media.paths
