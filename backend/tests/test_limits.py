from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from savetube.limits import next_window_start

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
