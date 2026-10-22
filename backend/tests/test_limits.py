from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from savetube.db.models import DownloadSettings
from savetube.limits import (
    backoff,
    day_start,
    next_day_start,
    next_window_start,
    window_label,
    window_opens,
)

KST = ZoneInfo("Asia/Seoul")


def kst(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=KST)


def test_no_window_never_waits():
    assert next_window_start(kst(21, 3), None, None) is None


def test_a_daytime_window():
    start, end = time(1, 0), time(5, 0)
    assert next_window_start(kst(21, 3), start, end) is None
    assert next_window_start(kst(21, 1), start, end) is None
    assert next_window_start(kst(21, 0, 30), start, end) == kst(21, 1)
    assert next_window_start(kst(21, 5), start, end) == kst(22, 1)
    assert next_window_start(kst(21, 6), start, end) == kst(22, 1)


def test_a_window_across_midnight():
    start, end = time(23, 0), time(5, 0)
    assert next_window_start(kst(21, 23, 30), start, end) is None
    assert next_window_start(kst(22, 4, 59), start, end) is None
    assert next_window_start(kst(22, 0, 0), start, end) is None
    assert next_window_start(kst(22, 5), start, end) == kst(22, 23)
    assert next_window_start(kst(21, 12), start, end) == kst(21, 23)
    assert next_window_start(kst(21, 22, 59), start, end) == kst(21, 23)


def test_the_window_is_read_in_seoul_time_whatever_the_clock_zone():
    start, end = time(23, 0), time(5, 0)
    # 14:30 UTC is 23:30 in Seoul.
    assert next_window_start(datetime(2026, 10, 21, 14, 30, tzinfo=UTC), start, end) is None
    # 21:00 UTC is 06:00 the next day in Seoul, so the window opens at 23:00 Seoul.
    assert next_window_start(datetime(2026, 10, 21, 21, 0, tzinfo=UTC), start, end) == kst(22, 23)


def test_the_block_backoff_doubles_from_30_minutes_up_to_12_hours():
    hours = [backoff(n) / timedelta(hours=1) for n in range(1, 8)]
    assert hours == [0.5, 1, 2, 4, 8, 12, 12]


def test_the_day_resets_at_midnight_in_seoul():
    assert day_start(kst(21, 0, 0)) == kst(21, 0)
    assert day_start(kst(21, 23, 59)) == kst(21, 0)
    assert next_day_start(kst(21, 0, 0)) == kst(22, 0)
    assert next_day_start(kst(21, 23, 59)) == kst(22, 0)
    # 15:30 UTC on the 21st is 00:30 on the 22nd in Seoul.
    assert day_start(datetime(2026, 10, 21, 15, 30, tzinfo=UTC)) == kst(22, 0)


def test_a_disabled_window_is_ignored_and_keeps_its_times():
    limits = DownloadSettings(window_start=time(23, 0), window_end=time(5, 0), window_enabled=False)
    assert window_opens(limits, kst(21, 12)) is None
    limits.window_enabled = True
    assert window_opens(limits, kst(21, 12)) == kst(21, 23)
    assert window_label(limits) == "23:00-05:00"
    limits.window_enabled = False
    assert window_label(limits) is None
