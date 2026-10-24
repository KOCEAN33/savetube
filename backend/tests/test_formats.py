"""The format watch's pick rules (docs/plan/live.md, Format watch), on the two
lives that prompted it."""

import json
from pathlib import Path

import pytest

from savetube import formats

DATA = Path(__file__).parent / "data"


def _vod(video_id):
    return json.loads((DATA / f"{video_id}.vod.json").read_text())


# The recordings' stored streams, as the production rows described them on
# 2026-10-24: 137+140 (H.264 Main 1080p30 about 1.1 Mbps, AAC) and 303+140
# (VP9 1080p60, AAC).
WBJ_RECORDING = {
    "format_id": "137+140",
    "requested_formats": [
        {"format_id": "137", "vcodec": "avc1.4d4028", "acodec": "none", "width": 1920,
         "height": 1080, "fps": 30, "tbr": 1100.0},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128.0},
    ],
}  # fmt: skip
HU7_RECORDING = {
    "format_id": "303+140",
    "requested_formats": [
        {"format_id": "303", "vcodec": "vp9", "acodec": "none", "width": 1920, "height": 1080,
         "fps": 60, "tbr": 3950.0},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128.0},
    ],
}  # fmt: skip


@pytest.mark.parametrize(
    ("video_id", "stored"), [("WBJeEquyZ2s", WBJ_RECORDING), ("hU7U_HnNWUo", HU7_RECORDING)]
)
def test_the_two_lives_replace_only_their_audio_with_251(video_id, stored):
    picks = formats.picks(formats.counted(_vod(video_id)["formats"]), stored)

    assert picks["audio"].above
    assert picks["audio"].format["format_id"] == "251"
    assert not picks["video"].above


def test_wbj_keeps_its_recorded_video_over_the_thinner_vod_stream():
    picks = formats.picks(formats.counted(_vod("WBJeEquyZ2s")["formats"]), WBJ_RECORDING)

    # The VOD's 137 is the same codec and size at 476 kbps against 1.1 Mbps.
    assert picks["video"].format["format_id"] == "137"
    assert "1.1 Mbps" in picks["video"].reason


def test_hu7_keeps_its_vp9_over_a_faster_h264():
    picks = formats.picks(formats.counted(_vod("hU7U_HnNWUo")["formats"]), HU7_RECORDING)

    # 4.6 Mbps H.264 against VP9: bitrates of different codecs are not compared.
    assert picks["video"].format["format_id"] == "299"
    assert picks["video"].reason == "the stored VP9 ranks over H.264"


def test_only_https_formats_without_drc_audio_count():
    ids = {f["format_id"] for f in formats.counted(_vod("hU7U_HnNWUo")["formats"])}

    assert ids == {"139", "249", "140", "251", "134", "298", "299"}


def _video(fid, vcodec, height, fps, tbr):
    return {"format_id": fid, "protocol": "https", "vcodec": vcodec, "acodec": "none",
            "height": height, "fps": fps, "tbr": tbr}  # fmt: skip


H264_1080P30 = _video("137", "avc1.640028", 1080, 30, 1100.0)


def test_a_vp9_stream_of_the_stored_size_replaces_h264_at_any_bitrate():
    vp9 = _video("248", "vp9", 1080, 30, 300.0)

    pick = formats.pick_video([vp9, _video("137", "avc1.640028", 1080, 30, 2000.0)], H264_1080P30)

    assert pick.above and pick.format["format_id"] == "248"


def test_av1_ranks_over_vp9():
    pick = formats.pick_video(
        [_video("248", "vp9", 1080, 30, 3000.0), _video("399", "av01.0.08M.08", 1080, 30, 900.0)],
        H264_1080P30,
    )

    assert pick.format["format_id"] == "399"


def test_a_better_codec_below_the_stored_height_or_fps_does_not_count():
    pick = formats.pick_video(
        [_video("247", "vp9", 720, 30, 1500.0), _video("303", "vp9", 1080, 25, 1500.0)],
        H264_1080P30,
    )

    assert pick.format is None and not pick.above


def test_a_higher_bitrate_of_the_same_codec_replaces():
    pick = formats.pick_video([_video("137", "avc1.640028", 1080, 30, 1500.0)], H264_1080P30)

    assert pick.above


def test_opus_ranks_over_aac_then_the_higher_abr():
    aac = {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 130.0}
    low = {"format_id": "250", "vcodec": "none", "acodec": "opus", "abr": 70.0}
    high = {"format_id": "251", "vcodec": "none", "acodec": "opus", "abr": 130.0}

    assert formats.pick_audio([aac, low, high], aac).format["format_id"] == "251"
    assert not formats.pick_audio([aac, low], high).above


@pytest.mark.parametrize(
    ("vod", "stored", "held"),
    [(100.0, 102.0, False), (100.0, 102.5, True), (103.0, 100.0, True), (None, 100.0, False)],
)
def test_a_duration_difference_over_2_s_holds(vod, stored, held):
    assert formats.held(vod, stored) is held
