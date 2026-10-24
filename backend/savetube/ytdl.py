"""yt-dlp embedded as a library. One YoutubeDL instance per download, never shared."""

import re
import shutil
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.plugins import load_all_plugins
from yt_dlp.postprocessor import PostProcessor

from savetube.layout import OUTTMPL
from savetube.logs import YtdlpLogger

DEFAULT_FORMAT = "bv*+ba/b"
# Resolution, then fps, then AV1 over VP9 over H.264. Fields given here go
# before the YouTube extractor's own, whose `source` field ranks Premium VP9
# (616) above AV1. Audio-only formats tie on all three, so the audio pick holds.
FORMAT_SORT = ["res", "fps", "vcodec:av01"]

# YoutubeDL loads plugins on first use, unlocked. Two threads creating the first
# instances at once import the bgutil plugin twice, and yt-dlp prints the failed
# registration as a raw traceback on stderr.
load_all_plugins()

# yt-dlp's `-t sleep` preset. The yt-dlp wiki puts the limits near 300
# videos/hour as a guest and 2000/hour logged in.
_SLEEP = {
    "sleep_interval_requests": 0.75,
    "sleep_interval": 10,
    "max_sleep_interval": 20,
    "sleep_interval_subtitles": 5,
}

# Seconds between retries while a scheduled stream has not started.
WAIT_FOR_VIDEO = (30, 120)


# yt-dlp's text when YouTube refuses this client rather than the video. Each
# group matches when all its lowercase parts are in the message. The bot check
# reads "Sign in to confirm you’re not a bot", with a straight or curly
# apostrophe, so no part includes it.
BLOCK_MARKERS = (("confirm you", "not a bot"), ("http error 429",))
# yt-dlp's text when the connection to the media host drops or times out, in
# the same groups as BLOCK_MARKERS. "4392287 bytes read, 5689076 more expected"
# is a body cut short.
NETWORK_MARKERS = (
    ("read timed out",),
    ("read operation timed out",),
    ("bytes read", "more expected"),
    ("connection reset",),
    ("connection refused",),
    ("remote end closed",),
    ("incompleteread",),
    ("network is unreachable",),
    ("failed to establish a new connection",),
)
# yt-dlp's text when googlevideo answers HTTP 500 to every retry of a stream,
# in the same groups as BLOCK_MARKERS. Some formats of some videos fail this way
# every time, from any network. An HLS download skips each fragment that keeps
# failing and ends on an empty file.
SERVER_ERROR_MARKERS = (("http error 500", "giving up after"), ("the downloaded file is empty",))
# YouTube's text for a members-only video the account may not watch, in both
# forms: "Join this channel to get access to members-only content like this
# video, and other exclusive perks." and "This video is available to this
# channel's members on level: ... Join this channel to get access to
# members-only content and other exclusive perks."
MEMBERS_ONLY_MARKERS = ("join this channel", "members-only content")
# yt-dlp's text when YouTube says only "Video unavailable", with no sentence
# after it, such as "DownloadError: ERROR: [youtube] <id>: Video unavailable".
# A throttled client gets this for public videos. A deleted video usually adds
# why, such as "This video has been removed by the uploader".
_BARE_UNAVAILABLE = re.compile(r"(?:\w+: )?(?:ERROR: )?\[youtube\] [\w-]+: Video unavailable\.?")


def youtube_block(message: str) -> bool:
    """Whether a failure is YouTube refusing this client."""
    text = message.lower()
    return any(all(part in text for part in group) for group in BLOCK_MARKERS)


def bare_unavailable(message: str) -> bool:
    """Whether a failure is YouTube's "Video unavailable" with no reason. It is
    a block or a deleted video, so alone it marks neither."""
    return _BARE_UNAVAILABLE.fullmatch(message.strip()) is not None


def network_error(message: str) -> bool:
    """Whether a failure is a dropped or timed-out connection."""
    text = message.lower()
    return any(all(part in text for part in group) for group in NETWORK_MARKERS)


def format_server_error(message: str) -> bool:
    """Whether googlevideo answered HTTP 500 to every retry of one stream."""
    text = message.lower()
    return any(all(part in text for part in group) for group in SERVER_ERROR_MARKERS)


def exclude_formats(selector: str, format_ids: list[str]) -> str:
    """`selector` with the formats in `format_ids` removed from its choices.

    A filter after a group drops the formats before any alternative inside it
    picks one. The id is quoted, because yt-dlp reads `format_id!=299` as a
    number comparison that keeps every format."""
    filters = "".join(
        "[format_id!='{}']".format(i.replace("\\", "\\\\").replace("'", "\\'")) for i in format_ids
    )
    return f"({selector.strip()}){filters}" if filters else selector


def members_only(message: str) -> bool:
    """Whether a failure is a members-only video the account may not watch."""
    text = message.lower()
    return any(marker in text for marker in MEMBERS_ONLY_MARKERS)


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def copy_cookies(src: Path, workdir: Path) -> Path:
    """yt-dlp writes the cookie jar back, and the Secret mount is read-only."""
    dest = workdir / "cookies.txt"
    shutil.copyfile(src, dest)
    dest.chmod(0o600)
    return dest


def build_params(
    workdir: Path,
    *,
    format: str = DEFAULT_FORMAT,
    cookiefile: Path | None = None,
    live: bool = False,
    ratelimit: int | None = None,
) -> dict[str, Any]:
    """Picklable, because a download child gets them through `spawn`. The child
    adds the logger and the hooks itself (`download`)."""
    params: dict[str, Any] = {
        "format": format,
        "merge_output_format": "mkv",
        "paths": {"home": str(workdir), "temp": str(workdir / ".tmp")},
        "outtmpl": {"default": OUTTMPL},
        "writeinfojson": True,
        "writethumbnail": True,
        # `all` includes the `live_chat` track, which is the chat replay.
        "writesubtitles": True,
        "subtitleslangs": ["all"],
        "noprogress": True,
        "quiet": True,
        "no_warnings": False,
        # The CLI defaults. As a library yt-dlp retries nothing without them, so
        # one dropped connection ended the run instead of resuming in place.
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        **_SLEEP,
    }
    if live:
        params["live_from_start"] = True
        params["wait_for_video"] = WAIT_FOR_VIDEO
        # A sleep before a DVR-off stream is footage lost.
        del params["sleep_interval"], params["max_sleep_interval"]
        # yt-dlp writes subtitles before the media, and a running live's chat
        # downloads until the stream ends. The VOD's archive keeps the replay.
        params["subtitleslangs"] = ["all", "-live_chat"]
    else:
        # A recording keeps yt-dlp's order: a live stream rarely offers AV1,
        # and a changed pick there cannot be tried again once the live ends.
        params["format_sort"] = FORMAT_SORT
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    if ratelimit is not None:
        # Bytes per second, per download.
        params["ratelimit"] = ratelimit
    return params


def list_entries(
    url: str, cookiefile: Path | None = None, limit: int | None = None
) -> dict[str, Any]:
    """A channel tab or playlist with flat entries: ids, titles, no per-video request.

    `approximate_date` turns "3 weeks ago" into an entry `timestamp`, which the
    channel's date cutoff reads. `limit` stops after that many entries, so only
    the pages holding them are requested.
    """
    params: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
        "quiet": True,
        "logger": YtdlpLogger(),
        "sleep_interval_requests": _SLEEP["sleep_interval_requests"],
    }
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    if limit is not None:
        params["playlistend"] = limit
    with yt_dlp.YoutubeDL(params) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def list_streams(channel_id: str, cookiefile: Path | None = None) -> dict[str, Any]:
    """The first page of the channel's Live tab: upcoming, live and past streams."""
    params: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "playlistend": 10,
        "quiet": True,
        "logger": YtdlpLogger(),
        "sleep_interval_requests": _SLEEP["sleep_interval_requests"],
    }
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(params) as ydl:
        return ydl.sanitize_info(
            ydl.extract_info(
                f"https://www.youtube.com/channel/{channel_id}/streams", download=False
            )
        )


def fetch_feed(channel_id: str) -> str:
    """The channel's RSS feed: its 15 newest uploads and scheduled streams."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode()


def _probe(url: str, cookiefile: Path | None, *, formats_required: bool = False) -> dict[str, Any]:
    params: dict[str, Any] = {
        "quiet": True,
        "logger": YtdlpLogger(),
        # An upcoming stream has no formats yet. Without this, yt-dlp raises
        # "This live event will begin in ..." instead of returning
        # `release_timestamp`.
        "ignore_no_formats_error": not formats_required,
        "sleep_interval_requests": _SLEEP["sleep_interval_requests"],
    }
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(params) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def probe_video(video_id: str, cookiefile: Path | None = None) -> dict[str, Any]:
    return _probe(video_url(video_id), cookiefile)


def probe_live(channel_id: str, cookiefile: Path | None = None) -> dict[str, Any]:
    """The stream live on the channel now. Raises "is not currently live" otherwise."""
    return _probe(f"https://www.youtube.com/channel/{channel_id}/live", cookiefile)


def probe_member(video_id: str, cookiefile: Path) -> dict[str, Any]:
    """A members-only video's info, formats included, read with one profile's
    cookies. Raises YouTube's members-only text when the account is no member."""
    return _probe(video_url(video_id), cookiefile, formats_required=True)


def probe_formats(video_id: str, cookiefile: Path | None) -> dict[str, Any]:
    """A stored live's info with the formats its VOD offers now. Raises when
    the video offers none, such as a live with no VOD."""
    return _probe(video_url(video_id), cookiefile, formats_required=True)


class NotSignedIn(Exception):
    def __init__(self) -> None:
        super().__init__("not signed in")


def read_account(cookiefile: Path) -> tuple[str, str | None]:
    """The name and handle of the YouTube account a writable cookie file copy
    signs in. Raises NotSignedIn for a logged-out file.

    yt-dlp has no public call for this, so it uses YoutubeTab's private
    methods (docs/traps.md)."""
    params = {"quiet": True, "logger": YtdlpLogger(), "cookiefile": str(cookiefile)}
    with yt_dlp.YoutubeDL(params) as ydl:
        ie = ydl.get_info_extractor("YoutubeTab")
        page = ie._download_webpage("https://www.youtube.com/", None, note=False)
        cfg = ie.extract_ytcfg(None, page)
        response = ie._extract_response(
            item_id="x",
            query={},
            ep="account/account_menu",
            ytcfg=cfg,
            headers=ie.generate_api_headers(ytcfg=cfg),
            note=False,
        )
    menu = response["actions"][0]["openPopupAction"]["popup"]["multiPageMenuRenderer"]
    header = (menu.get("header") or {}).get("activeAccountHeaderRenderer")
    if header is None:
        raise NotSignedIn
    return header["accountName"]["simpleText"], header.get("channelHandle", {}).get("simpleText")


def avatar_url(listing: dict[str, Any]) -> str | None:
    """The channel's profile picture from a tab listing's thumbnails: the square
    one, else the uncropped original, which can be megabytes."""
    thumbnails = listing.get("thumbnails") or []
    square = [t for t in thumbnails if t.get("width") and t.get("width") == t.get("height")]
    if square:
        return min(square, key=lambda t: t["width"])["url"]
    return next((t["url"] for t in thumbnails if t.get("id") == "avatar_uncropped"), None)


class _BeforeDownload(PostProcessor):
    def __init__(self, on_format: Callable[[dict[str, Any]], None]) -> None:
        super().__init__()
        self._on_format = on_format

    def run(self, info: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
        self._on_format(info)
        return [], info


def download(
    url: str,
    params: dict[str, Any],
    *,
    on_format: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Download one video and return its sanitised info dict. `on_format` sees
    the info dict once the format is chosen, before the download starts."""
    params = {**params, "logger": YtdlpLogger()}
    with yt_dlp.YoutubeDL(params) as ydl:
        if on_format is not None:
            ydl.add_post_processor(_BeforeDownload(on_format), when="before_dl")
        info = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info)
