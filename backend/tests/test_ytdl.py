import pytest

from savetube.storage import finalize
from savetube.ytdl import WAIT_FOR_VIDEO, build_params, copy_cookies


def test_archive_params_sleep_and_never_record_from_start(tmp_path):
    p = build_params(tmp_path)
    assert p["format"] == "bv*+ba/b"
    assert p["merge_output_format"] == "mkv"
    assert p["outtmpl"] == {"default": "%(id)s.%(ext)s"}
    assert p["paths"] == {"home": str(tmp_path), "temp": str(tmp_path / ".tmp")}
    assert p["sleep_interval"] == 10 and p["max_sleep_interval"] == 20
    assert "live_from_start" not in p
    assert "cookiefile" not in p


def test_live_params_record_from_start_without_pre_download_sleep(tmp_path):
    p = build_params(tmp_path, live=True)
    assert p["live_from_start"] is True
    assert p["wait_for_video"] == WAIT_FOR_VIDEO
    assert "sleep_interval" not in p and "max_sleep_interval" not in p
    assert p["sleep_interval_requests"] == 0.75


def test_cookie_copy_is_writable_and_passed(tmp_path):
    src = tmp_path / "secret.txt"
    src.write_text("# Netscape HTTP Cookie File\n")
    src.chmod(0o400)
    work = tmp_path / "work"
    work.mkdir()
    copied = copy_cookies(src, work)
    assert copied.read_text() == "# Netscape HTTP Cookie File\n"
    copied.write_text("rewritten")  # yt-dlp writes the jar back
    assert build_params(work, cookiefile=copied)["cookiefile"] == str(copied)


def test_finalize_moves_and_refuses_to_overwrite(tmp_path):
    incoming = tmp_path / ".incoming" / "7"
    (incoming / ".tmp").mkdir(parents=True)
    (incoming / "vid.mkv").write_bytes(b"x")
    final = tmp_path / "C [UC1]" / "videos" / "2026-01-01 T [vid]"
    finalize(incoming, final)
    assert (final / "vid.mkv").read_bytes() == b"x"
    assert not (final / ".tmp").exists()
    assert not incoming.exists()

    other = tmp_path / ".incoming" / "8"
    other.mkdir()
    with pytest.raises(FileExistsError):
        finalize(other, final)
    assert other.exists()
