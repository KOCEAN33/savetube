"""Where a video lands in the archive tree: `<channel>/<type>/<video>/`."""

import enum
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from yt_dlp.utils import sanitize_filename

# ZFS and every Linux filesystem cap one path component at 255 bytes.
NAME_MAX_BYTES = 255
# Files inside a video directory are named `<video id>.<ext>`, so only
# directory names carry a title and need the byte cap.
OUTTMPL = "%(id)s.%(ext)s"

LIVE_STATUSES = frozenset({"is_live", "was_live", "post_live"})


class VideoType(enum.StrEnum):
    VIDEOS = "videos"
    SHORTS = "shorts"
    LIVES = "lives"
    MEMBERS = "members"


def classify(info: Mapping[str, Any], *, from_shorts_tab: bool = False) -> VideoType:
    """First match wins: members, then lives, then shorts, then videos."""
    if info.get("availability") == "subscriber_only":
        return VideoType.MEMBERS
    if info.get("live_status") in LIVE_STATUSES:
        return VideoType.LIVES
    if from_shorts_tab:
        return VideoType.SHORTS
    return VideoType.VIDEOS


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode(errors="ignore").rstrip()


def _name(label: str, suffix: str, prefix: str = "") -> str:
    """`<prefix><label> <suffix>`, with the label cut so the whole fits NAME_MAX_BYTES."""
    label = sanitize_filename(label).strip() or "_"
    budget = NAME_MAX_BYTES - len(f"{prefix} {suffix}".encode())
    return f"{prefix}{_truncate_utf8(label, budget)} {suffix}"


def channel_dir_name(name: str, channel_id: str) -> str:
    return _name(name, f"[{channel_id}]")


def video_dir_name(upload_date: str | None, title: str, video_id: str) -> str:
    """`upload_date` is yt-dlp's `YYYYMMDD` string."""
    if upload_date and len(upload_date) == 8 and upload_date.isdigit():
        prefix = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]} "
    else:
        prefix = "0000-00-00 "
    return _name(title, f"[{video_id}]", prefix)


def video_dir(channel_dir: str, video_type: VideoType, info: Mapping[str, Any]) -> PurePosixPath:
    """The video's directory relative to the archive root."""
    return PurePosixPath(
        channel_dir,
        video_type.value,
        video_dir_name(info.get("upload_date"), info.get("title") or info["id"], info["id"]),
    )
