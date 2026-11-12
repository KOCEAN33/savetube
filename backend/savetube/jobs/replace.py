"""The step of a replace job that takes the place of an archive run's move
into the tree (docs/plan/live.md, Format watch). savetube.jobs.job runs
the job like an archive job and calls `prepare`, then `swap_in`.

The job downloads only the streams it names and remuxes them with the kept
stream of the stored file through `ffmpeg -c copy`. The result passes the
download's integrity checks, then takes the stored file's place in the tree.
The first stored file moves aside until the watch ends, or for ORIGINAL_KEPT
after a swap outside an open watch."""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import audio, formats, integrity
from savetube.config import Settings
from savetube.db.models import EncodeJob, EncodeState, FormatWatch, Job, JobState, Video
from savetube.storage import dir_size
from savetube.watch import ORIGINAL_KEPT, is_open, media_path, originals_dir, probe_duration


class ReplaceFailed(Exception):
    pass


@dataclass
class Prepared:
    """A remuxed and checked result waiting for the swap."""

    stored: Path
    out: Path
    # The downloaded streams by kind, `video` and `audio`.
    new: dict[str, dict[str, Any]]
    checks: list[str]
    keep_original: bool
    old_info: dict[str, Any]


async def prepare(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    video_id: str,
    outdir: Path,
    downloaded: Path,
    info: dict[str, Any],
    streams: list[dict[str, Any]],
    tools: integrity.Tools,
    step: Callable[[str], Awaitable[None]],
) -> Prepared:
    """Remux the verified download with the stored file's other stream into
    `outdir`, on the archive's filesystem so the swap is a rename, and check
    the result."""
    async with sessionmaker() as session:
        video = await session.get(Video, video_id)
        watch = await session.get(FormatWatch, video_id)
        stored = media_path(settings.archive_root, video)
        keep_original = watch.original_path is None
        old_info = video.info or {}
    if stored is None or not stored.is_file():
        raise ReplaceFailed("stored file not found")
    new = formats.stored_streams(formats.with_sizes(info, streams))
    out = outdir / f"{video_id}.mkv"
    await step("merging")
    await asyncio.to_thread(
        _remux, stored, downloaded, out, video="video" in new, audio="audio" in new
    )
    await step("verifying")
    try:
        # The stream copy reads every packet of the result.
        checks = await asyncio.to_thread(
            integrity.verify,
            out,
            {"vcodec": "kept", "acodec": "kept"},
            [],
            resumed=True,
            tools=tools,
        )
    except integrity.IntegrityFailed as e:
        raise ReplaceFailed(f"integrity check failed: {e}") from None
    return Prepared(stored, out, new, checks, keep_original, old_info)


async def swap_in(
    session: AsyncSession, settings: Settings, job: Job, prepared: Prepared, tools: integrity.Tools
) -> tuple[Video, FormatWatch]:
    """Put the result in the stored file's place and write the video, its
    watch and the job as succeeded. The caller holds the job row's lock and
    commits."""
    video_id = job.video_id
    final = prepared.stored.with_name(prepared.out.name)
    aside = await asyncio.to_thread(
        _swap,
        settings.archive_root,
        video_id,
        prepared.stored,
        prepared.out,
        keep_original=prepared.keep_original,
    )
    await audio.add(final, tools)
    merged = _merged_info(prepared.old_info, prepared.new, final)
    _write_info_json(final.parent / f"{video_id}.info.json", merged)
    video = await session.get(Video, video_id)
    watch = await session.get(FormatWatch, video_id)
    video.info = merged
    video.format_id = merged.get("format_id")
    video.size_bytes = dir_size(final.parent)
    video.verified_at = datetime.now(UTC)
    video.verified_checks = prepared.checks
    if aside is not None:
        watch.original_path = aside.relative_to(settings.archive_root).as_posix()
    # The latest swap restarts the keep, so the user can compare with the
    # original for a week after each pick.
    if watch.original_path is not None and not is_open(watch, video.verified_at):
        watch.original_until = video.verified_at + ORIGINAL_KEPT
    try:
        watch.stored_duration = await asyncio.to_thread(probe_duration, final, tools)
    except integrity.IntegrityFailed:
        watch.stored_duration = None
    if watch.stored_duration is not None:
        video.duration = round(watch.stored_duration)
    if "video" in prepared.new:
        # The stored video stream is no longer an encode's result, so the
        # video may be encoded again (savetube.encode.candidates).
        await session.execute(
            update(EncodeJob)
            .where(EncodeJob.video_id == video_id, EncodeJob.state == EncodeState.SUCCEEDED)
            .values(state=EncodeState.SUPERSEDED)
        )
    job.state = JobState.SUCCEEDED
    job.finished_at = video.verified_at
    job.progress = None
    return video, watch


def _remux(stored: Path, downloaded: Path, out: Path, *, video: bool, audio: bool) -> None:
    """Write `out` from the downloaded streams and the stored file's other
    stream, without re-encoding, into a fresh directory."""
    if shutil.which("ffmpeg") is None:
        raise ReplaceFailed("ffmpeg not found")
    shutil.rmtree(out.parent, ignore_errors=True)
    out.parent.mkdir(parents=True)
    maps = ["-map", "1:v:0" if video else "0:v:0", "-map", "1:a:0" if audio else "0:a:0"]
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(stored), "-i", str(downloaded),
         *maps, "-c", "copy", "-f", "matroska", str(out)],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise ReplaceFailed(result.stderr.strip()[-500:] or f"ffmpeg exit code {result.returncode}")


def _swap(
    archive_root: Path, video_id: str, stored: Path, out: Path, *, keep_original: bool
) -> Path | None:
    """Put `out` in the stored file's place, named `<video id><out's suffix>`.
    With `keep_original`, the stored file first gets a hard link in the
    originals directory, which this returns. Otherwise the stored file came
    from an earlier replace and goes. The link comes before the rename, so no
    step leaves the tree without the file. An old audio copy goes too: the
    caller writes a new one."""
    aside = None
    if keep_original:
        aside = originals_dir(archive_root, video_id) / stored.name
        aside.parent.mkdir(parents=True, exist_ok=True)
        # A link left by a run cut off before its commit is the original.
        if not aside.exists():
            os.link(stored, aside)
    final = stored.with_name(f"{video_id}{out.suffix}")
    os.replace(out, final)
    if stored != final:
        stored.unlink(missing_ok=True)
    for suffix in audio.SUFFIXES:
        old = final.with_suffix(suffix)
        if old != final:
            old.unlink(missing_ok=True)
    return aside


def _merged_info(
    old: dict[str, Any], new: dict[str, dict[str, Any]], final: Path
) -> dict[str, Any]:
    """The stored info dict with the replaced streams' format fields."""
    streams = {**formats.stored_streams(old), **{k: formats.trim(v) for k, v in new.items()}}
    ordered = [streams[k] for k in ("video", "audio") if k in streams]
    merged = {**old, "requested_formats": ordered, "ext": final.suffix.lstrip(".")}
    merged["format_id"] = "+".join(str(f.get("format_id")) for f in ordered)
    merged["format"] = " + ".join(str(f.get("format") or f.get("format_id")) for f in ordered)
    if "video" in streams:
        v = streams["video"]
        for key in ("vcodec", "width", "height", "fps", "resolution", "dynamic_range", "vbr"):
            merged[key] = v.get(key)
    if "audio" in streams:
        a = streams["audio"]
        for key in ("acodec", "abr", "asr", "audio_channels"):
            merged[key] = a.get(key)
    merged["tbr"] = sum(f.get("tbr") or 0 for f in ordered) or None
    return merged


def _write_info_json(path: Path, info: dict[str, Any]) -> None:
    """The `.info.json` beside the video is the source of truth for a rescan."""
    if not path.is_file():
        return
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False))
    os.replace(tmp, path)
