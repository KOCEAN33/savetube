"""The format pick of the format watch (docs/plan/live.md, Format watch).

Audio and video are judged apart. Only `https` formats count, and audio
formats with `-drc` in the id do not."""

from dataclasses import dataclass
from typing import Any, Literal

Kind = Literal["audio", "video"]

# The format keys the watch stores and compares. The rest of a yt-dlp format
# dict is URLs and headers.
FIELDS = (
    "format_id",
    "format",
    "format_note",
    "ext",
    "protocol",
    "vcodec",
    "acodec",
    "width",
    "height",
    "fps",
    "tbr",
    "abr",
    "vbr",
    "asr",
    "audio_channels",
    "dynamic_range",
    "resolution",
    "filesize",
    "filesize_approx",
)


def _has(codec: str | None) -> bool:
    # "none" is a format without that stream. None is unknown.
    return codec not in (None, "none")


def kind(f: dict[str, Any]) -> Kind | None:
    """`audio` for an audio-only format, `video` for a video-only one, None
    for a muxed or unknown format."""
    audio, video = _has(f.get("acodec")), _has(f.get("vcodec"))
    if audio and f.get("vcodec") == "none":
        return "audio"
    if video and f.get("acodec") == "none":
        return "video"
    return None


def trim(f: dict[str, Any]) -> dict[str, Any]:
    return {k: f[k] for k in FIELDS if f.get(k) is not None}


def counted(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The audio-only and video-only `https` formats, without `-drc` audio,
    trimmed to FIELDS. HLS copies of the same streams are left out."""
    out = []
    for f in formats:
        k = kind(f)
        if k is None or f.get("protocol") != "https":
            continue
        if k == "audio" and "-drc" in str(f.get("format_id")):
            continue
        out.append(trim(f))
    return out


def stored_streams(info: dict[str, Any]) -> dict[Kind, dict[str, Any]]:
    """The stored file's video and audio format, from its info dict."""
    out: dict[Kind, dict[str, Any]] = {}
    for f in info.get("requested_formats") or [info]:
        if _has(f.get("vcodec")):
            out.setdefault("video", f)
        if _has(f.get("acodec")):
            out.setdefault("audio", f)
    return out


def video_codec(vcodec: str | None) -> tuple[int, str]:
    """AV1 over VP9 over H.264, and a display name."""
    v = (vcodec or "").lower()
    if v.startswith("av01"):
        return 3, "AV1"
    if v.startswith(("vp9", "vp09")):
        return 2, "VP9"
    if v.startswith(("avc1", "h264")):
        return 1, "H.264"
    return 0, vcodec or "unknown"


def audio_codec(acodec: str | None) -> tuple[int, str]:
    """Opus over AAC, and a display name."""
    a = (acodec or "").lower()
    if a.startswith("opus"):
        return 2, "Opus"
    if a.startswith(("mp4a", "aac")):
        return 1, "AAC"
    return 0, acodec or "unknown"


def kbps(rate: float | None) -> str:
    if rate is None:
        return "unknown bitrate"
    return f"{rate / 1000:.1f} Mbps" if rate >= 1000 else f"{rate:.0f} kbps"


def _label(f: dict[str, Any]) -> str:
    height, fps = f.get("height"), f.get("fps")
    return f"{height}p{round(fps) if fps else ''}" if height else "unknown size"


@dataclass(frozen=True)
class Pick:
    kind: Kind
    # The best counted format, None when no format qualifies.
    format: dict[str, Any] | None
    # Whether it ranks above the stored stream, which queues a replace.
    above: bool
    reason: str


def pick_audio(formats: list[dict[str, Any]], stored: dict[str, Any] | None) -> Pick:
    """Opus over AAC, then the higher `abr`."""
    candidates = [f for f in formats if kind(f) == "audio"]
    if not candidates:
        return Pick("audio", None, False, "no audio format offered")
    best = max(candidates, key=lambda f: (audio_codec(f.get("acodec"))[0], f.get("abr") or 0))
    rank, name = audio_codec(best.get("acodec"))
    if stored is None:
        return Pick("audio", best, True, f"{name} where the stored file has no audio")
    s_rank, s_name = audio_codec(stored.get("acodec"))
    if rank > s_rank:
        return Pick("audio", best, True, f"{name} over the stored {s_name}")
    if rank < s_rank:
        return Pick("audio", best, False, f"the stored {s_name} ranks over {name}")
    abr, s_abr = best.get("abr"), stored.get("abr")
    if abr is not None and s_abr is not None and abr > s_abr:
        return Pick("audio", best, True, f"{name} {kbps(abr)} over the stored {kbps(s_abr)}")
    return Pick("audio", best, False, f"the stored {s_name} {kbps(s_abr)} is not below {kbps(abr)}")


def pick_video(formats: list[dict[str, Any]], stored: dict[str, Any] | None) -> Pick:
    """Among formats with height and fps at least the stored stream's, AV1
    over VP9 over H.264, then the higher `tbr` within one codec. Bitrates of
    different codecs are not compared."""
    s_height = (stored or {}).get("height")
    s_fps = (stored or {}).get("fps")
    candidates = [
        f
        for f in formats
        if kind(f) == "video"
        and (s_height is None or (f.get("height") or 0) >= s_height)
        and (s_fps is None or (f.get("fps") or 0) >= s_fps)
    ]
    if not candidates:
        return Pick("video", None, False, f"no format reaches the stored {_label(stored or {})}")
    best = max(candidates, key=lambda f: (video_codec(f.get("vcodec"))[0], f.get("tbr") or 0))
    rank, name = video_codec(best.get("vcodec"))
    if stored is None:
        return Pick("video", best, True, f"{name} where the stored file has no video")
    s_rank, s_name = video_codec(stored.get("vcodec"))
    if rank > s_rank:
        return Pick("video", best, True, f"{name} over the stored {s_name}")
    if rank < s_rank:
        return Pick("video", best, False, f"the stored {s_name} ranks over {name}")
    tbr, s_tbr = best.get("tbr"), stored.get("tbr")
    if tbr is not None and s_tbr is not None and tbr > s_tbr:
        return Pick("video", best, True, f"{name} {kbps(tbr)} over the stored {kbps(s_tbr)}")
    return Pick("video", best, False, f"the stored {s_name} {kbps(s_tbr)} is not below {kbps(tbr)}")


def picks(formats: list[dict[str, Any]], info: dict[str, Any]) -> dict[Kind, Pick]:
    stored = stored_streams(info)
    return {
        "audio": pick_audio(formats, stored.get("audio")),
        "video": pick_video(formats, stored.get("video")),
    }


# The VOD and the stored file may differ by this much before a replace holds.
# Fixed, unlike the 1% of the download check.
HOLD_SECONDS = 2


def held(vod: float | None, stored: float | None) -> bool:
    return vod is not None and stored is not None and abs(vod - stored) > HOLD_SECONDS
