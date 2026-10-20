"""Source URLs: a channel tab or a playlist, in one canonical form each, so the
unique `source.url` catches a second add of the same source."""

import enum
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

from savetube.db.models import SourceKind

_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"})
# `/@handle`, `/channel/UC...`, and the legacy `/c/name` and `/user/name`.
_CHANNEL = re.compile(r"^/(@[^/]+|channel/UC[\w-]{22}|c/[^/]+|user/[^/]+)(?:/([^/]+))?/?$")
_PLAYLIST_ID = re.compile(r"^[\w-]+$")


class Tab(enum.StrEnum):
    VIDEOS = "videos"
    SHORTS = "shorts"
    # YouTube's path for the Live tab: current, upcoming and past streams.
    STREAMS = "streams"


class InvalidSourceUrl(ValueError):
    pass


@dataclass(frozen=True)
class SourceUrl:
    url: str
    kind: SourceKind


def parse(url: str, tabs: list[Tab] | None = None) -> list[SourceUrl]:
    """One source per tab for a channel URL, or the one playlist.

    A channel URL that names a tab yields that tab. A bare channel URL yields
    `tabs`, and an empty `tabs` is an error, because a bare channel URL lists
    the tabs, not videos.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or parts.hostname not in _HOSTS:
        raise InvalidSourceUrl("not a youtube.com URL")
    playlist = parse_qs(parts.query).get("list")
    if playlist:
        if not _PLAYLIST_ID.match(playlist[0]):
            raise InvalidSourceUrl("malformed playlist id")
        return [
            SourceUrl(f"https://www.youtube.com/playlist?list={playlist[0]}", SourceKind.PLAYLIST)
        ]
    # A pasted `/@한글` arrives percent-encoded or not, depending on the browser.
    m = _CHANNEL.match(unquote(parts.path))
    if not m:
        raise InvalidSourceUrl("neither a channel nor a playlist URL")
    base, tab = m.groups()
    if tab is not None:
        try:
            tabs = [Tab(tab)]
        except ValueError:
            raise InvalidSourceUrl(f"unsupported channel tab {tab!r}") from None
    if not tabs:
        raise InvalidSourceUrl("a channel URL needs at least one tab")
    return [
        SourceUrl(f"https://www.youtube.com/{base}/{t.value}", SourceKind.TAB)
        for t in dict.fromkeys(tabs)
    ]
