from datetime import UTC, datetime, timedelta

from savetube.staging import MIN_FREE_BYTES, MOVE_BATCH_BYTES, move_reason

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
GIB = 1024**3


def reason(*, staged=GIB, free=30 * GIB, running=0, waited=timedelta(minutes=5)):
    return move_reason(staged, free, running, NOW - waited, NOW)


def test_a_full_batch_moves_while_a_download_runs():
    assert reason(staged=MOVE_BATCH_BYTES, running=1) == "batch"
    assert reason(staged=MOVE_BATCH_BYTES - 1, running=1) is None


def test_low_free_space_moves_whatever_waits():
    assert reason(free=MIN_FREE_BYTES - 1, running=1) == "space"
    assert reason(free=MIN_FREE_BYTES, running=1) is None


def test_an_idle_hour_without_downloads_moves_a_small_batch():
    assert reason(waited=timedelta(hours=1)) == "idle"
    assert reason(waited=timedelta(minutes=59)) is None
    assert reason(waited=timedelta(hours=2), running=1) is None


def test_nothing_staged_never_moves():
    assert move_reason(0, 0, 0, None, NOW) is None
