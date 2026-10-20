"""yt-dlp embedded as a library. One YoutubeDL instance per download, never shared."""

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from savetube.layout import OUTTMPL

DEFAULT_FORMAT = "bv*+ba/b"

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
    progress_hook: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
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
        **_SLEEP,
    }
    if live:
        params["live_from_start"] = True
        params["wait_for_video"] = WAIT_FOR_VIDEO
        # A sleep before a DVR-off stream is footage lost.
        del params["sleep_interval"], params["max_sleep_interval"]
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    if progress_hook is not None:
        params["progress_hooks"] = [progress_hook]
    return params


def list_entries(url: str, cookiefile: Path | None = None) -> dict[str, Any]:
    """A channel tab or playlist with flat entries: ids, titles, no per-video request.

    `approximate_date` turns "3 weeks ago" into an entry `timestamp`, which the
    rule's date cutoff reads.
    """
    params: dict[str, Any] = {
        "extract_flat": "in_playlist",
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
        "quiet": True,
        "sleep_interval_requests": _SLEEP["sleep_interval_requests"],
    }
    if cookiefile is not None:
        params["cookiefile"] = str(cookiefile)
    with yt_dlp.YoutubeDL(params) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def download(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """Download one video and return its sanitised info dict."""
    with yt_dlp.YoutubeDL(params) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.sanitize_info(info)
