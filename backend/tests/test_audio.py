import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from savetube.audio import AudioFailed, Skipped, extract, target
from savetube.integrity import Tools

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg or ffprobe not on PATH",
)


def make_media(path: Path, *, audio: str | None = "libopus", video: bool = True) -> Path:
    """A one-second file with a test pattern and a sine tone."""
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    if video:
        args += ["-f", "lavfi", "-i", "testsrc=d=1:s=32x32:r=5"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=d=1"]
    if video:
        args += ["-c:v", "mpeg4"]
    if audio:
        args += ["-c:a", audio, "-ar", "48000"]
    subprocess.run([*args, str(path)], check=True)
    return path


def _streams(path: Path) -> list[tuple[str, str]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [(s["codec_type"], s["codec_name"]) for s in json.loads(out.stdout)["streams"]]


@needs_ffmpeg
@pytest.mark.parametrize(
    ("encoder", "codec", "ext"),
    [("libopus", "opus", "opus"), ("aac", "aac", "m4a"), ("flac", "flac", "mka")],
)
def test_the_audio_stream_is_copied_beside_the_video_by_codec(tmp_path, encoder, codec, ext):
    media = make_media(tmp_path / "v1.mkv", audio=encoder)
    written = extract(media, Tools())
    assert written == tmp_path / f"v1.{ext}"
    assert _streams(written) == [("audio", codec)]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["v1.mkv", f"v1.{ext}"])


@needs_ffmpeg
def test_a_dry_target_names_the_file_and_writes_nothing(tmp_path):
    media = make_media(tmp_path / "v1.mkv", audio="aac")
    assert target(media, Tools()) == tmp_path / "v1.m4a"
    assert [p.name for p in tmp_path.iterdir()] == ["v1.mkv"]


@needs_ffmpeg
@pytest.mark.parametrize(
    ("video", "audio", "reason"),
    [(False, "libopus", "no video stream"), (True, None, "no audio stream")],
)
def test_a_file_without_video_or_audio_is_skipped(tmp_path, video, audio, reason):
    media = make_media(tmp_path / "v1.mkv", audio=audio, video=video)
    with pytest.raises(Skipped) as e:
        extract(media, Tools())
    assert str(e.value) == reason
    assert [p.name for p in tmp_path.iterdir()] == ["v1.mkv"]


class _Probe(Tools):
    def probe(self, path):
        return {"streams": [{"codec_type": "video"}, {"codec_type": "audio", "codec_name": "opus"}]}


def test_a_failed_copy_leaves_no_file_behind(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        '#!/bin/bash\nfor a; do last="$a"; done\nprintf half > "$last"\n'
        "echo 'Invalid data found' >&2\nexit 1\n"
    )
    ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    work = tmp_path / "work"
    work.mkdir()
    media = work / "v1.mkv"
    media.write_bytes(b"not media")
    with pytest.raises(AudioFailed) as e:
        extract(media, _Probe())
    assert "Invalid data found" in str(e.value)
    assert [p.name for p in work.iterdir()] == ["v1.mkv"]


def test_without_ffprobe_nothing_is_written(tmp_path):
    class Missing(Tools):
        def probe(self, path):
            return None

    media = tmp_path / "v1.mkv"
    media.write_bytes(b"m")
    with pytest.raises(Skipped) as e:
        extract(media, Missing())
    assert str(e.value) == "ffprobe not found"
