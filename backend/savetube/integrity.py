"""Integrity checks of a finished archive download, before it moves into the tree
(docs/plan/archive.md, Resuming and verifying downloads)."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from savetube.logs import emit

log = logging.getLogger(__name__)

# The merged file's duration may differ from the info dict's by this much.
DURATION_FLOOR_SECONDS = 2
DURATION_SHARE = 0.01


class IntegrityFailed(Exception):
    """`check` names the failed check, `numbers` what it measured."""

    def __init__(self, check: str, **numbers: Any) -> None:
        super().__init__(f"{check} " + " ".join(f"{k}={v}" for k, v in numbers.items()))
        self.check = check
        self.numbers = numbers


class Tools:
    """ffprobe and ffmpeg. Each returns None when its binary is missing."""

    def probe(self, path: Path) -> dict[str, Any] | None:
        if shutil.which("ffprobe") is None:
            return None
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            raise IntegrityFailed("probe", errors=out.stderr.strip()[-500:])
        return json.loads(out.stdout)

    def packets(self, path: Path) -> str | None:
        """ffmpeg's error output from a stream copy of every packet. A copy
        decodes nothing, so a multi-hour file takes about as long as a read."""
        if shutil.which("ffmpeg") is None:
            return None
        out = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-i",
                str(path),
                "-c",
                "copy",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0 and not out.stderr.strip():
            return f"ffmpeg exit code {out.returncode}"
        return out.stderr


def _check_sizes(streams: list[dict[str, Any]]) -> bool:
    """Whether any stream carried an exact size to compare."""
    checked = False
    for s in streams:
        if s.get("expected") is None:
            continue
        checked = True
        if s["size"] != s["expected"]:
            raise IntegrityFailed(
                "size", format=s["format_id"], size=s["size"], expected=s["expected"]
            )
    return checked


def _check_probe(probed: dict[str, Any], info: dict[str, Any]) -> None:
    kinds = {s.get("codec_type") for s in probed.get("streams") or []}
    for kind, codec in (("video", info.get("vcodec")), ("audio", info.get("acodec"))):
        # "none" is a format without that stream. None is unknown.
        if codec not in (None, "none") and kind not in kinds:
            raise IntegrityFailed("streams", missing=kind)
    expected = info.get("duration")
    found = (probed.get("format") or {}).get("duration")
    if expected and found is not None:
        found = float(found)
        allowed = max(DURATION_FLOOR_SECONDS, expected * DURATION_SHARE)
        if abs(found - expected) > allowed:
            raise IntegrityFailed("duration", found=round(found, 1), expected=expected)


def verify(
    media: Path,
    info: dict[str, Any],
    streams: list[dict[str, Any]],
    *,
    resumed: bool,
    tools: Tools,
) -> list[str]:
    """Raise IntegrityFailed, else return the checks that ran.

    `streams` holds each downloaded stream's size before the merge, with the
    format's exact `filesize` as `expected` or None. Only a resumed download
    gets the packet pass, because a fresh one was written in one go.
    """
    checks = []
    if _check_sizes(streams):
        checks.append("size")
    if not media.is_file():
        raise IntegrityFailed("file", path=media.name)
    probed = tools.probe(media)
    if probed is None:
        emit(log, logging.DEBUG, "download.verify_skipped", reason="ffprobe not found")
        return checks
    _check_probe(probed, info)
    checks.append("probe")
    if resumed:
        errors = tools.packets(media)
        if errors is None:
            emit(log, logging.DEBUG, "download.verify_skipped", reason="ffmpeg not found")
        elif errors.strip():
            raise IntegrityFailed("packets", errors=errors.strip()[-500:])
        else:
            checks.append("packets")
    return checks
