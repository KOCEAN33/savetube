"""External re-encoding through job files (docs/plan/encode.md).

The scheduler keeps up to QUEUE_DEPTH job files in `.encode/queue/` for an
encoder that reads the archive over Samba. It follows each job as the encoder
moves its file through `claimed/` and `work/` into `done/` or `failed/`. A
done result passes the profile's checks, then takes the stored file's place by
rename, with the stored file kept aside as a replace keeps it
(savetube.jobs.replace)."""

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from savetube import audio, formats, logs
from savetube.config import Settings
from savetube.db.models import (
    EncodeJob,
    EncodeSettings,
    EncodeState,
    FormatWatch,
    Job,
    JobKind,
    JobState,
    Video,
    VideoState,
)
from savetube.integrity import IntegrityFailed, Tools
from savetube.jobs.replace import _merged_info, _swap, _write_info_json
from savetube.jobs.rows import LOG_TAIL_CHARS
from savetube.logs import emit, human_bytes
from savetube.storage import dir_size
from savetube.watch import ORIGINAL_KEPT, is_open, media_path, probe_duration

log = logging.getLogger(__name__)

# On the archive's filesystem, so a done result renames into the tree.
ENCODE_DIR = ".encode"
FOLDERS = ("queue", "claimed", "work", "done", "failed")
# Job files waiting in queue/. The encoder takes one at a time, and a short
# queue lets a settings change or a cancel act soon.
QUEUE_DEPTH = 3
# An open job whose file no folder holds for this long is lost. The encoder
# moves a file between folders in moments.
LOST_AFTER = timedelta(minutes=10)
DURATION_SECONDS = 1.0
FPS_SLACK = 0.05

_OPEN = (EncodeState.QUEUED, EncodeState.CLAIMED)
# A video with one of these is never queued again: an open job, a failure
# until a user requeues it, or a result already swapped in.
_BLOCKING = (*_OPEN, EncodeState.FAILED, EncodeState.SUCCEEDED)


@dataclass(frozen=True)
class Profile:
    # yt-dlp's vcodec for the stored info dict, and ffprobe's codec_name.
    vcodec: str
    codec_name: str
    ten_bit: bool
    max_fps: float


PROFILES = {"vtuber-av1-cq46": Profile(vcodec="av01", codec_name="av1", ten_bit=True, max_fps=30)}


class Rejected(Exception):
    """The result failed a check. It moves to failed/ with this reason."""


def encode_root(archive_root: Path) -> Path:
    return archive_root / ENCODE_DIR


def _settings_row(row: EncodeSettings | None) -> EncodeSettings:
    return row or EncodeSettings(id=1, enabled=False, profile=next(iter(PROFILES)))


async def step(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    *,
    tools: Tools,
    lock: asyncio.Lock,
    skipped: set[str],
    now: datetime | None = None,
) -> None:
    """The scheduler's minute: follow the open jobs, swap the done results,
    then top the queue up. `lock` keeps a cancel from the API out of the
    middle. `skipped` holds the video ids the format check ruled out, so
    each is read once per scheduler run."""
    now = now or datetime.now(UTC)
    root = encode_root(settings.archive_root)
    if not settings.archive_root.is_dir():
        emit(log, logging.ERROR, "encode.unmounted", root=str(settings.archive_root))
        return
    for folder in FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)
    async with lock:
        await track(sessionmaker, settings, tools=tools, now=now)
        await fill(sessionmaker, settings, skipped=skipped, now=now)


def _listing(root: Path) -> dict[str, set[int]]:
    """The job ids in each folder. Listed in the order a file moves, so a file
    that moves during the listing is found at least once."""
    wanted = {
        "queue": ".json",
        "claimed": ".json",
        "work": ".mkv",
        "done": ".json",
        "failed": ".json",
    }
    out: dict[str, set[int]] = {}
    for folder, suffix in wanted.items():
        out[folder] = {
            int(p.stem)
            for p in (root / folder).iterdir()
            if p.suffix == suffix and p.stem.isdigit() and p.is_file()
        }
    return out


async def track(
    sessionmaker: async_sessionmaker, settings: Settings, *, tools: Tools, now: datetime
) -> None:
    root = encode_root(settings.archive_root)
    found = _listing(root)
    async with sessionmaker() as session:
        jobs = {
            j.id: j
            for j in await session.scalars(
                select(EncodeJob).where(EncodeJob.state.in_([*_OPEN, EncodeState.LOST]))
            )
        }
        for job in jobs.values():
            if job.id in found["done"] or job.id in found["failed"]:
                continue
            if job.state is EncodeState.LOST:
                continue
            if job.id in found["claimed"] or job.id in found["work"]:
                if job.state is EncodeState.QUEUED:
                    emit(log, logging.INFO, "encode.claimed", job=job.id, video=job.video_id)
                job.state = EncodeState.CLAIMED
                job.seen_at = now
            elif job.id in found["queue"]:
                job.seen_at = now
            elif (job.seen_at or job.created_at) <= now - LOST_AFTER:
                job.state = EncodeState.LOST
                job.finished_at = now
                emit(log, logging.WARNING, "encode.lost", job=job.id, video=job.video_id)
        await session.commit()
        # A requeued failure's files go, so failed/ keeps only what waits for the user.
        requeued = set(
            await session.scalars(
                select(EncodeJob.id).where(
                    EncodeJob.id.in_(found["failed"]), EncodeJob.state == EncodeState.REQUEUED
                )
            )
        )
    for job_id in requeued:
        for suffix in (".json", ".mkv"):
            (root / "failed" / f"{job_id}{suffix}").unlink(missing_ok=True)
    for job_id in sorted(found["failed"]):
        if job_id in jobs:
            await _record_failure(sessionmaker, root, job_id, now)
    for job_id in sorted(found["done"]):
        with logs.context(job=job_id):
            try:
                await _take_done(sessionmaker, settings, root, job_id, jobs.get(job_id), tools, now)
            except Exception as e:
                emit(
                    log,
                    logging.ERROR,
                    "encode.swap_failed",
                    job=job_id,
                    reason=logs.cut(f"{type(e).__name__}: {e}"),
                    exc_info=True,
                )


def _read_job_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return {"unreadable": f"{type(e).__name__}: {e}"}
    return data if isinstance(data, dict) else {"unreadable": "not a JSON object"}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write beside the target and rename, so a reader never sees half a file.
    The temporary name has no `.json` suffix, so no listing counts it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, path)


async def _record_failure(
    sessionmaker: async_sessionmaker, root: Path, job_id: int, now: datetime
) -> None:
    data = await asyncio.to_thread(_read_job_file, root / "failed" / f"{job_id}.json")
    async with sessionmaker() as session:
        job = await session.get(EncodeJob, job_id)
        job.state = EncodeState.FAILED
        job.reason = str(data.get("reason") or data.get("unreadable") or "no reason given")
        job.log_tail = str(data["log_tail"])[-LOG_TAIL_CHARS:] if data.get("log_tail") else None
        job.finished_at = now
        await session.commit()
    emit(
        log,
        logging.WARNING,
        "encode.failed",
        job=job_id,
        video=job.video_id,
        reason=logs.cut(job.reason),
    )


async def _take_done(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    root: Path,
    job_id: int,
    job: EncodeJob | None,
    tools: Tools,
    now: datetime,
) -> None:
    done_json = root / "done" / f"{job_id}.json"
    result = root / "done" / f"{job_id}.mkv"
    data = await asyncio.to_thread(_read_job_file, done_json)
    if job is None:
        await asyncio.to_thread(_to_failed, root, job_id, data, "no open job with this id", now)
        emit(log, logging.WARNING, "encode.rejected", job=job_id, reason="no open job")
        return
    async with sessionmaker() as session:
        if await _busy(session, job.video_id):
            # A replace or a recording may change the stored file. The next
            # minute looks again.
            return
    try:
        checked = await _check(sessionmaker, settings, job, data, result, tools)
        size = await _swap_in(sessionmaker, settings, job, data, result, checked, tools, now)
    except Rejected as e:
        reason = str(e)
        await asyncio.to_thread(_to_failed, root, job_id, data, reason, now)
        async with sessionmaker() as session:
            row = await session.get(EncodeJob, job_id)
            row.state = EncodeState.FAILED
            row.reason = reason
            row.finished_at = now
            _take_fields(row, data)
            await session.commit()
        emit(log, logging.WARNING, "encode.rejected", job=job_id, video=job.video_id, reason=reason)
        return
    done_json.unlink(missing_ok=True)
    emit(
        log,
        logging.INFO,
        "encode.swapped",
        job=job_id,
        video=job.video_id,
        profile=job.profile,
        source=human_bytes(job.source_size),
        result=human_bytes(size),
    )


def _to_failed(root: Path, job_id: int, data: dict[str, Any], reason: str, now: datetime) -> None:
    """Move a done result to failed/, the `.mkv` first and the job file with
    the reason last, as the encoder writes done/."""
    result = root / "done" / f"{job_id}.mkv"
    if result.exists():
        os.replace(result, root / "failed" / result.name)
    _write_json(
        root / "failed" / f"{job_id}.json",
        {**data, "reason": reason, "log_tail": None, "failed_at": now.isoformat()},
    )
    (root / "done" / f"{job_id}.json").unlink(missing_ok=True)


def _take_fields(job: EncodeJob, data: dict[str, Any]) -> None:
    job.encoder = str(data["encoder"]) if data.get("encoder") else None
    job.ffmpeg_version = str(data["ffmpeg_version"]) if data.get("ffmpeg_version") else None
    if isinstance(data.get("size"), int):
        job.result_size = data["size"]


async def _busy(session: AsyncSession, video_id: str) -> bool:
    """A replace or a recording queued or running for the video."""
    return (
        await session.scalar(
            select(Job.id)
            .where(
                Job.video_id == video_id,
                Job.kind.in_([JobKind.REPLACE, JobKind.RECORD]),
                Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
            )
            .limit(1)
        )
        is not None
    )


def _sha256(path: Path) -> tuple[int, str]:
    """The file's size and SHA-256 hex."""
    with path.open("rb") as f:
        return os.fstat(f.fileno()).st_size, hashlib.file_digest(f, "sha256").hexdigest()


def _rate(value: str | None) -> float | None:
    try:
        rate = Fraction(value or "")
    except ValueError, ZeroDivisionError:
        return None
    return float(rate) if rate else None


def _streams(probed: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [s for s in probed.get("streams") or [] if s.get("codec_type") == kind]


@dataclass
class _Checked:
    video: dict[str, Any]
    duration: float
    checks: list[str]


async def _check(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    job: EncodeJob,
    data: dict[str, Any],
    result: Path,
    tools: Tools,
) -> _Checked:
    """Raise Rejected unless the result is the profile's encode of the stored
    file it was queued for."""
    if "unreadable" in data:
        raise Rejected(f"job file unreadable: {data['unreadable']}")
    if data.get("profile") != job.profile:
        raise Rejected(f"profile {data.get('profile')!r}, queued {job.profile!r}")
    profile = PROFILES.get(job.profile)
    if profile is None:
        raise Rejected(f"unknown profile {job.profile!r}")
    try:
        size, digest = await asyncio.to_thread(_sha256, result)
    except FileNotFoundError:
        raise Rejected("result file missing") from None
    if data.get("size") != size:
        raise Rejected(f"size {size}, job file says {data.get('size')}")
    if digest != str(data.get("sha256", "")).lower():
        raise Rejected("sha256 mismatch")
    if size >= job.source_size:
        raise Rejected(f"result {size} bytes is not smaller than the source {job.source_size}")
    source = settings.archive_root / job.source_path
    if not source.is_file():
        raise Rejected("stored file missing")
    try:
        new = await asyncio.to_thread(tools.probe, result)
        old = await asyncio.to_thread(tools.probe, source)
    except IntegrityFailed as e:
        raise Rejected(str(e)) from None
    if new is None or old is None:
        raise Rejected("ffprobe not found")
    [video, *_] = _streams(new, "video") or [None]
    [source_video, *_] = _streams(old, "video") or [None]
    if video is None or source_video is None:
        raise Rejected("no video stream")
    audio_streams = len(_streams(new, "audio"))
    if audio_streams == 0 or audio_streams != len(_streams(old, "audio")):
        raise Rejected(f"{audio_streams} audio streams, source has {len(_streams(old, 'audio'))}")
    if video.get("codec_name") != profile.codec_name:
        raise Rejected(f"video codec {video.get('codec_name')}, profile wants {profile.codec_name}")
    if profile.ten_bit and "10" not in str(video.get("pix_fmt")):
        raise Rejected(f"pixel format {video.get('pix_fmt')} is not 10-bit")
    dims, source_dims = ((s.get("width"), s.get("height")) for s in (video, source_video))
    if dims != source_dims:
        raise Rejected(f"resolution {dims}, source {source_dims}")
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    source_fps = _rate(source_video.get("avg_frame_rate")) or _rate(
        source_video.get("r_frame_rate")
    )
    if fps is None or source_fps is None:
        raise Rejected("frame rate unknown")
    want = min(source_fps, profile.max_fps)
    if abs(fps - want) > FPS_SLACK:
        raise Rejected(f"{fps:.2f} fps, profile wants {want:.2f}")
    duration = float((new.get("format") or {}).get("duration") or 0)
    source_duration = float((old.get("format") or {}).get("duration") or 0)
    if abs(duration - source_duration) > DURATION_SECONDS:
        raise Rejected(f"duration {duration:.1f} s, source {source_duration:.1f} s")
    errors = await asyncio.to_thread(tools.packets, result)
    if errors is None:
        raise Rejected("ffmpeg not found")
    if errors.strip():
        raise Rejected(f"packets: {errors.strip()[-500:]}")
    async with sessionmaker() as session:
        stored = await session.get(Video, job.video_id)
        old_info = stored.info or {}
    audio_kbps = sum(
        f.get("abr") or f.get("tbr") or 0
        for kind, f in formats.stored_streams(old_info).items()
        if kind == "audio"
    )
    total_kbps = size * 8 / 1000 / duration if duration else 0
    vbr = round(max(total_kbps - audio_kbps, 0), 3) or None
    width, height = dims
    stream = {
        # Not a YouTube format id: the profile name records the re-encode, so
        # a probe never takes the stored stream for a YouTube download.
        "format_id": job.profile,
        "format": f"{job.profile} - {width}x{height}",
        "format_note": "re-encoded",
        "protocol": "encode",
        "vcodec": profile.vcodec,
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "vbr": vbr,
        "tbr": vbr,
        "resolution": f"{width}x{height}",
        "dynamic_range": formats.stored_streams(old_info).get("video", {}).get("dynamic_range"),
    }
    return _Checked(video=stream, duration=duration, checks=["sha256", "probe", "packets"])


async def _swap_in(
    sessionmaker: async_sessionmaker,
    settings: Settings,
    job: EncodeJob,
    data: dict[str, Any],
    result: Path,
    checked: _Checked,
    tools: Tools,
    now: datetime,
) -> int:
    async with sessionmaker() as session:
        video = await session.get(Video, job.video_id, with_for_update=True)
        stored = media_path(settings.archive_root, video)
        if (
            video.state is not VideoState.COMPLETE
            or stored is None
            or stored.relative_to(settings.archive_root).as_posix() != job.source_path
            or not stored.is_file()
            or stored.stat().st_size != job.source_size
        ):
            raise Rejected("the stored file changed since the job was queued")
        watch = await session.get(FormatWatch, job.video_id)
        if watch is None:
            # A row without an automatic watch holds the original's keep.
            watch = FormatWatch(video_id=job.video_id, started_at=None)
            session.add(watch)
        final = stored.with_name(f"{job.video_id}.mkv")
        aside = await asyncio.to_thread(
            _swap,
            settings.archive_root,
            job.video_id,
            stored,
            result,
            keep_original=watch.original_path is None,
        )
        await audio.add(final, tools)
        merged = _merged_info(video.info or {}, {"video": checked.video}, final)
        _write_info_json(final.parent / f"{job.video_id}.info.json", merged)
        video.info = merged
        video.format_id = merged.get("format_id")
        video.size_bytes = dir_size(final.parent)
        video.verified_at = now
        video.verified_checks = checked.checks
        if aside is not None:
            watch.original_path = aside.relative_to(settings.archive_root).as_posix()
        if watch.original_path is not None and not is_open(watch, now):
            watch.original_until = now + ORIGINAL_KEPT
        try:
            watch.stored_duration = await asyncio.to_thread(probe_duration, final, tools)
        except IntegrityFailed:
            watch.stored_duration = None
        row = await session.get(EncodeJob, job.id)
        row.state = EncodeState.SUCCEEDED
        row.finished_at = now
        _take_fields(row, data)
        await session.commit()
        return final.stat().st_size


def _youtube_av1(video: Video, watch: FormatWatch | None) -> bool:
    """Whether the last format list offers AV1 at the stored height or above:
    the format watch's last probe, else the download's own list. A video
    with neither counts as offered, because nothing shows that it is not."""
    if watch is not None and watch.formats is not None:
        listed = watch.formats
    else:
        listed = formats.counted((video.info or {}).get("formats") or [])
    if not listed:
        return True
    height = video.video_height or 0
    return any(
        formats.kind(f) == "video"
        and formats.video_codec(f.get("vcodec"))[1] == "AV1"
        and (f.get("height") or 0) >= height
        for f in listed
    )


def candidates():
    """Complete H.264 videos that no replace, recording, open format watch,
    kept original or encode job holds, largest first."""
    held_by_job = (
        select(Job.id)
        .where(
            Job.video_id == Video.id,
            Job.kind.in_([JobKind.REPLACE, JobKind.RECORD]),
            Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .exists()
    )
    held_by_watch = (
        select(FormatWatch.video_id)
        .where(
            FormatWatch.video_id == Video.id,
            FormatWatch.original_path.is_not(None)
            | (FormatWatch.started_at.is_not(None) & FormatWatch.ended_at.is_(None)),
        )
        .exists()
    )
    held_by_encode = (
        select(EncodeJob.id)
        .where(EncodeJob.video_id == Video.id, EncodeJob.state.in_(_BLOCKING))
        .exists()
    )
    codec = func.lower(Video.video_codec)
    return (
        select(Video.id)
        .where(
            Video.state == VideoState.COMPLETE,
            Video.dir_path.is_not(None),
            codec.like("avc1%") | codec.like("h264%"),
            ~held_by_job,
            ~held_by_watch,
            ~held_by_encode,
        )
        .order_by(Video.size_bytes.desc().nulls_last(), Video.id)
    )


async def fill(
    sessionmaker: async_sessionmaker, settings: Settings, *, skipped: set[str], now: datetime
) -> None:
    root = encode_root(settings.archive_root)
    async with sessionmaker() as session:
        config = _settings_row(await session.get(EncodeSettings, 1))
        if not config.enabled or config.profile not in PROFILES:
            return
        queued = await session.scalar(
            select(func.count()).select_from(EncodeJob).where(EncodeJob.state == EncodeState.QUEUED)
        )
        room = QUEUE_DEPTH - queued
        if room <= 0:
            return
        for video_id in await session.scalars(candidates()):
            if room == 0:
                break
            if video_id in skipped:
                continue
            video = await session.get(Video, video_id)
            watch = await session.get(FormatWatch, video_id)
            stored = media_path(settings.archive_root, video)
            if _youtube_av1(video, watch) or stored is None or not stored.is_file():
                skipped.add(video_id)
                # The info dict is large. Keep the session from holding each one.
                session.expunge(video)
                continue
            job = EncodeJob(
                video_id=video_id,
                profile=config.profile,
                source_path=stored.relative_to(settings.archive_root).as_posix(),
                source_size=stored.stat().st_size,
                created_at=now,
                seen_at=now,
            )
            session.add(job)
            await session.flush()
            # The file before the commit: a crash between them leaves a job
            # file no row knows, whose result the scheduler moves to failed/.
            await asyncio.to_thread(
                _write_json,
                root / "queue" / f"{job.id}.json",
                {
                    "job_id": job.id,
                    "video_id": video_id,
                    "source": job.source_path,
                    "profile": job.profile,
                    "created_at": now.isoformat(),
                },
            )
            await session.commit()
            room -= 1
            emit(
                log,
                logging.INFO,
                "encode.queued",
                job=job.id,
                video=video_id,
                profile=job.profile,
                size=human_bytes(job.source_size),
            )


class NotCancellable(Exception):
    pass


async def cancel(
    sessionmaker: async_sessionmaker, settings: Settings, job_id: int, *, lock: asyncio.Lock
) -> None:
    """Delete a queued job's file. Raises LookupError for an unknown job and
    NotCancellable once the encoder claimed it."""
    async with lock, sessionmaker() as session:
        job = await session.get(EncodeJob, job_id)
        if job is None:
            raise LookupError(job_id)
        if job.state is not EncodeState.QUEUED:
            raise NotCancellable(job.state.value)
        try:
            (encode_root(settings.archive_root) / "queue" / f"{job_id}.json").unlink()
        except FileNotFoundError:
            # The encoder renamed it into claimed/ since the last scan.
            raise NotCancellable("claimed") from None
        job.state = EncodeState.CANCELLED
        job.finished_at = datetime.now(UTC)
        await session.commit()
    emit(log, logging.INFO, "encode.cancelled", job=job_id, video=job.video_id)
