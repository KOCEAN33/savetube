import pytest

from savetube.db.models import SourceKind
from savetube.sources import InvalidSourceUrl, SourceUrl, Tab, parse

UC = "UCLA_DiR1FfKNvjuUpBHmylQ"


def test_bare_channel_yields_one_source_per_tab():
    assert parse("https://youtube.com/@NASA/", [Tab.VIDEOS, Tab.STREAMS, Tab.VIDEOS]) == [
        SourceUrl("https://www.youtube.com/@NASA/videos", SourceKind.TAB),
        SourceUrl("https://www.youtube.com/@NASA/streams", SourceKind.TAB),
    ]


def test_tab_in_url_wins_over_tabs():
    assert parse(f"https://m.youtube.com/channel/{UC}/shorts", [Tab.VIDEOS]) == [
        SourceUrl(f"https://www.youtube.com/channel/{UC}/shorts", SourceKind.TAB)
    ]


def test_encoded_handle_matches_the_plain_one():
    assert parse("https://www.youtube.com/@%ED%95%9C%EA%B8%80/videos") == parse(
        "https://www.youtube.com/@한글/videos"
    )


def test_list_parameter_makes_a_playlist():
    for url in (
        "https://www.youtube.com/playlist?list=PLF98rxtslw6s",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLF98rxtslw6s&index=2",
    ):
        assert parse(url, [Tab.VIDEOS]) == [
            SourceUrl("https://www.youtube.com/playlist?list=PLF98rxtslw6s", SourceKind.PLAYLIST)
        ]


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://www.youtube.com/@NASA", "at least one tab"),
        ("https://www.youtube.com/@NASA/community", "unsupported channel tab"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "neither"),
        ("https://evil.example/@NASA/videos", "not a youtube.com URL"),
        ("https://www.youtube.com/playlist?list=PL'1", "malformed"),
    ],
)
def test_rejects(url, message):
    with pytest.raises(InvalidSourceUrl, match=message):
        parse(url)


def test_playlists_tab_makes_a_playlists_source():
    assert parse(f"https://www.youtube.com/channel/{UC}/playlists") == [
        SourceUrl(f"https://www.youtube.com/channel/{UC}/playlists", SourceKind.PLAYLISTS)
    ]
    assert parse("https://www.youtube.com/@NASA", [Tab.VIDEOS, Tab.PLAYLISTS]) == [
        SourceUrl("https://www.youtube.com/@NASA/videos", SourceKind.TAB),
        SourceUrl("https://www.youtube.com/@NASA/playlists", SourceKind.PLAYLISTS),
    ]
