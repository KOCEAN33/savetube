"""The archive's local time zone. It imports nothing from savetube, so any module can use it."""

from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo("Asia/Seoul")
