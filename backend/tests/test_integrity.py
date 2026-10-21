from pathlib import Path

import pytest

from savetube.integrity import IntegrityFailed, Tools, verify

MERGED = {"duration": 3600, "vcodec": "avc1.640028", "acodec": "opus"}
AUDIO_ONLY = {"duration": 60, "vcodec": "none", "acodec": "mp4a.40.2"}


def _probe(duration, *kinds):
    return {
        "format": {"duration": str(duration)},
        "streams": [{"codec_type": k} for k in kinds],
    }


class _Tools(Tools):
    """ffprobe and ffmpeg replaced by canned answers, each call recorded."""

    def __init__(self, probed=None, errors="", missing=False):
        self.calls = []
        self._probed, self._errors, self._missing = probed, errors, missing

    def probe(self, path):
        self.calls.append(("probe", path))
        return None if self._missing else self._probed

    def packets(self, path):
        self.calls.append(("packets", path))
        return None if self._missing else self._errors


def _media(tmp_path) -> Path:
    path = tmp_path / "v.mkv"
    path.write_bytes(b"media")
    return path


def test_a_stream_shorter_than_its_exact_filesize_fails(tmp_path):
    streams = [
        {"format_id": "137", "size": 1000, "expected": 1000},
        {"format_id": "251", "size": 999, "expected": 1000},
    ]
    with pytest.raises(IntegrityFailed) as e:
        verify(
            _media(tmp_path),
            MERGED,
            streams,
            resumed=False,
            tools=_Tools(_probe(3600, "video", "audio")),
        )
    assert e.value.check == "size"
    assert e.value.numbers == {"format": "251", "size": 999, "expected": 1000}


def test_a_stream_without_an_exact_filesize_is_not_size_checked(tmp_path):
    streams = [{"format_id": "251", "size": 999, "expected": None}]
    checks = verify(
        _media(tmp_path),
        MERGED,
        streams,
        resumed=False,
        tools=_Tools(_probe(3600, "video", "audio")),
    )
    assert checks == ["probe"]


@pytest.mark.parametrize(
    ("info", "probed", "ok"),
    [
        # 1% of an hour is 36 s.
        (MERGED, _probe(3635.9, "video", "audio"), True),
        (MERGED, _probe(3637, "video", "audio"), False),
        (MERGED, _probe(3563.5, "video", "audio"), False),
        # Under 200 s the floor of 2 s holds.
        (AUDIO_ONLY, _probe(61.9, "audio"), True),
        (AUDIO_ONLY, _probe(62.5, "audio"), False),
    ],
)
def test_the_merged_file_lasts_within_2_s_or_1_percent_of_the_info_duration(
    tmp_path, info, probed, ok
):
    tools = _Tools(probed)
    if ok:
        assert verify(_media(tmp_path), info, [], resumed=False, tools=tools) == ["probe"]
    else:
        with pytest.raises(IntegrityFailed) as e:
            verify(_media(tmp_path), info, [], resumed=False, tools=tools)
        assert e.value.check == "duration"
        assert e.value.numbers["expected"] == info["duration"]


def test_the_merged_file_carries_the_streams_its_format_promised(tmp_path):
    with pytest.raises(IntegrityFailed) as e:
        verify(_media(tmp_path), MERGED, [], resumed=False, tools=_Tools(_probe(3600, "video")))
    assert e.value.check == "streams"
    assert e.value.numbers == {"missing": "audio"}
    # An audio-only format needs no video stream.
    assert verify(
        _media(tmp_path), AUDIO_ONLY, [], resumed=False, tools=_Tools(_probe(60, "audio"))
    ) == ["probe"]


def test_an_unknown_duration_checks_only_the_streams(tmp_path):
    info = {**MERGED, "duration": None}
    assert verify(
        _media(tmp_path), info, [], resumed=False, tools=_Tools(_probe(5, "video", "audio"))
    ) == ["probe"]


def test_only_a_resumed_download_gets_the_packet_pass(tmp_path):
    media = _media(tmp_path)
    fresh = _Tools(_probe(3600, "video", "audio"), errors="corrupt packet")
    assert verify(media, MERGED, [], resumed=False, tools=fresh) == ["probe"]
    assert ("packets", media) not in fresh.calls

    clean = _Tools(_probe(3600, "video", "audio"))
    assert verify(media, MERGED, [], resumed=True, tools=clean) == ["probe", "packets"]

    broken = _Tools(_probe(3600, "video", "audio"), errors="[mkv] Invalid EBML number\n")
    with pytest.raises(IntegrityFailed) as e:
        verify(media, MERGED, [], resumed=True, tools=broken)
    assert e.value.check == "packets"
    assert e.value.numbers == {"errors": "[mkv] Invalid EBML number"}


def test_without_ffprobe_and_ffmpeg_only_the_sizes_are_checked(tmp_path):
    streams = [{"format_id": "140", "size": 10, "expected": 10}]
    tools = _Tools(missing=True)
    assert verify(_media(tmp_path), MERGED, streams, resumed=True, tools=tools) == ["size"]


def test_a_missing_media_file_fails(tmp_path):
    with pytest.raises(IntegrityFailed) as e:
        verify(tmp_path / "gone.mkv", MERGED, [], resumed=False, tools=_Tools(_probe(3600)))
    assert e.value.check == "file"
