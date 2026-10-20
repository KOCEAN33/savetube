from pathlib import PurePosixPath

import pytest

from savetube.layout import (
    NAME_MAX_BYTES,
    VideoType,
    channel_dir_name,
    classify,
    video_dir,
    video_dir_name,
)


@pytest.mark.parametrize(
    ("info", "from_shorts_tab", "expected"),
    [
        ({"availability": "subscriber_only", "live_status": "was_live"}, False, VideoType.MEMBERS),
        ({"availability": "public", "live_status": "was_live"}, True, VideoType.LIVES),
        ({"live_status": "is_live"}, False, VideoType.LIVES),
        ({"live_status": "post_live"}, False, VideoType.LIVES),
        ({"live_status": "not_live"}, True, VideoType.SHORTS),
        ({"live_status": "not_live", "availability": "public"}, False, VideoType.VIDEOS),
        ({}, False, VideoType.VIDEOS),
    ],
)
def test_classify_first_match_wins(info, from_shorts_tab, expected):
    assert classify(info, from_shorts_tab=from_shorts_tab) is expected


def test_video_dir_name_formats_date_title_and_id():
    assert video_dir_name("20260315", "First stream", "dQw4w9WgXcQ") == (
        "2026-03-15 First stream [dQw4w9WgXcQ]"
    )


def test_video_dir_name_without_date():
    assert video_dir_name(None, "x", "abc") == "0000-00-00 x [abc]"


def test_slash_in_title_never_splits_the_path():
    name = video_dir_name("20260101", "a/b", "id1")
    assert "/" not in name


def test_long_utf8_title_is_cut_on_a_character_boundary():
    title = "가" * 200  # 600 bytes in UTF-8
    name = video_dir_name("20260101", title, "dQw4w9WgXcQ")
    assert len(name.encode()) <= NAME_MAX_BYTES
    assert name.startswith("2026-01-01 가")
    assert name.endswith(" [dQw4w9WgXcQ]")
    name.encode().decode()  # no half character left behind


def test_channel_dir_name():
    assert channel_dir_name("Some Channel", "UCabc") == "Some Channel [UCabc]"


def test_video_dir_joins_channel_type_and_video():
    info = {"id": "vid1", "title": "T", "upload_date": "20250102"}
    assert video_dir("C [UC1]", VideoType.LIVES, info) == PurePosixPath(
        "C [UC1]/lives/2025-01-02 T [vid1]"
    )
