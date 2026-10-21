import logging
from datetime import UTC, datetime

import pytest

from savetube import logs

# 02:44:58 UTC is 11:44:58 in Asia/Seoul.
CREATED = datetime(2026, 10, 21, 2, 44, 58, 400000, tzinfo=UTC).timestamp()


def _record(name, level, msg, fields=None, args=()):
    record = logging.LogRecord(name, level, __file__, 1, msg, args, None)
    record.created = CREATED
    if fields is not None:
        record.savetube_fields = fields
    return record


def _format(record):
    return logs.LogfmtFormatter().format(record)


def test_an_event_line_is_logfmt_in_seoul_time():
    line = _format(
        _record("savetube.jobs", logging.INFO, "job.start", {"job": 230, "video": "Kd9PnKbqdE4"})
    )
    assert (
        line == "ts=2026-10-21T11:44:58+09:00 level=info event=job.start job=230 video=Kd9PnKbqdE4"
    )


def test_values_are_quoted_and_escaped_and_none_is_dropped():
    fields = {
        "title": "歌枠 part 1",
        "reason": 'said "no"\nthen \\ left',
        "empty": "",
        "eq": "a=b",
        "size": None,
    }
    line = _format(_record("savetube.jobs", logging.WARNING, "job.failed", fields))
    assert line == (
        "ts=2026-10-21T11:44:58+09:00 level=warning event=job.failed"
        ' title="歌枠 part 1" reason="said \\"no\\"\\nthen \\\\ left" empty="" eq="a=b"'
    )


def test_a_library_line_carries_logger_and_msg():
    line = _format(_record("procrastinate.worker", logging.WARNING, "Deferred %d job", args=(1,)))
    assert line == (
        "ts=2026-10-21T11:44:58+09:00 level=warning"
        ' logger=procrastinate.worker msg="Deferred 1 job"'
    )


def test_context_fields_come_before_the_records_own():
    with logs.context(job=7, video="abc"):
        line = _format(_record("savetube.jobs", logging.INFO, "download.merge", {"step": "Merger"}))
        with logs.context(title="t"):
            inner = _format(_record("x", logging.INFO, "e.v", {}))
    after = _format(_record("x", logging.INFO, "e.v", {}))
    assert line.endswith("event=download.merge job=7 video=abc step=Merger")
    assert inner.endswith("event=e.v job=7 video=abc title=t")
    assert after.endswith("event=e.v")


def test_a_traceback_follows_the_line():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("savetube.scheduler", logging.ERROR, "loop.failed", {"loop": "poll"})
        record.exc_info = sys.exc_info()
    first, *rest = _format(record).split("\n")
    assert first == "ts=2026-10-21T11:44:58+09:00 level=error event=loop.failed loop=poll"
    assert rest[0] == "Traceback (most recent call last):"
    assert rest[-1] == "ValueError: boom"


def test_emit_names_the_event_and_keeps_field_order(caplog):
    log = logging.getLogger("savetube.test")
    with caplog.at_level(logging.INFO, logger="savetube.test"):
        logs.emit(log, logging.INFO, "dispatch.sent", jobs="241,242", running=1, max=2)
    [record] = caplog.records
    assert record.getMessage() == "dispatch.sent"
    assert list(record.savetube_fields.items()) == [("jobs", "241,242"), ("running", 1), ("max", 2)]


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    root.handlers[:], level = saved
    root.setLevel(level)
    for name in ("savetube", "alembic"):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_configure_keeps_libraries_at_warning_and_alembic_at_info(restore_logging):
    logs.configure("INFO")
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("savetube").level == logging.INFO
    assert logging.getLogger("alembic").level == logging.INFO
    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, logs.LogfmtFormatter)

    logs.configure("debug")
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("savetube").level == logging.DEBUG
    assert len(logging.getLogger().handlers) == 1


def test_yt_dlp_warnings_stay_warnings_and_its_errors_drop_to_debug(caplog):
    ytdlp = logs.YtdlpLogger()
    with caplog.at_level(logging.DEBUG, logger="savetube.ytdlp"):
        ytdlp.debug("[youtube] Kd9PnKbqdE4: Downloading webpage")
        ytdlp.warning("WARNING: [youtube] no impersonate target")
        ytdlp.error("ERROR: [youtube] Video unavailable")
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("savetube.ytdlp", logging.DEBUG, "[youtube] Kd9PnKbqdE4: Downloading webpage"),
        ("savetube.ytdlp", logging.WARNING, "WARNING: [youtube] no impersonate target"),
        ("savetube.ytdlp", logging.DEBUG, "ERROR: [youtube] Video unavailable"),
    ]


def test_sizes_and_durations_read_as_humans_write_them():
    assert logs.human_bytes(900) == "900B"
    assert logs.human_bytes(1024**2) == "1MiB"
    assert logs.human_bytes(int(1.42 * 1024**3)) == "1.42GiB"
    assert logs.human_duration(6) == "6s"
    assert logs.human_duration(252) == "4m12s"
    assert logs.human_duration(3725) == "1h2m5s"


DOWNLOADING = {
    "status": "downloading",
    "downloaded_bytes": 1024**2,
    "total_bytes": 4 * 1024**2,
    "speed": 512 * 1024.0,
    "eta": 6,
    "info_dict": {"format_id": "140"},
}


def test_progress_reads_the_total_then_the_estimate_then_the_fragments():
    assert logs.progress(DOWNLOADING) == {
        "format": "140",
        "pct": 25.0,
        "done": 1024**2,
        "total": 4 * 1024**2,
        "speed": 512 * 1024.0,
        "eta": 6,
    }
    estimate = {**DOWNLOADING, "total_bytes": None, "total_bytes_estimate": 2 * 1024**2}
    assert logs.progress(estimate)["pct"] == 50.0
    assert logs.progress(estimate)["total"] == 2 * 1024**2
    frags = {
        "status": "downloading",
        "downloaded_bytes": 10,
        "fragment_index": 3,
        "fragment_count": 12,
    }
    assert logs.progress(frags)["pct"] == 25.0
    assert logs.progress({"status": "downloading", "downloaded_bytes": 10})["pct"] is None


def test_the_progress_line_shows_at_most_once_a_minute(caplog):
    now = [1000.0]
    hook = logs.ProgressLog(clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="savetube.download"):
        hook(DOWNLOADING)
        now[0] += 30
        hook(DOWNLOADING)
        now[0] += 31
        hook({**DOWNLOADING, "status": "finished"})
        hook(DOWNLOADING)
    lines = [_format(r).split(" ", 1)[1] for r in caplog.records]
    assert lines == [
        "level=info event=download.progress format=140 pct=25.0% done=1MiB total=4MiB"
        " speed=512KiB/s eta=6s",
        "level=info event=download.progress format=140 pct=25.0% done=1MiB total=4MiB"
        " speed=512KiB/s eta=6s",
    ]
