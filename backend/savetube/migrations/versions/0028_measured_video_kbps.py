"""video_kbps from the measured stream size, video_protocol, and the stored
duration of recordings

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-01 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0028'
down_revision: str | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _stream(codec: str) -> str:
    return (
        "(CASE WHEN info->'requested_formats'->0 IS NULL THEN info"
        " ELSE jsonb_path_query_first(info,"
        f" '$.requested_formats[*] ? (@.{codec} != null && @.{codec} != \"none\")') END)"
    )


def _number(stream: str, key: str) -> str:
    return f"jsonb_path_query_first({stream}, '$.{key} ? (@.type() == \"number\")')::numeric"


_V = _stream("vcodec")
_KBPS = (
    f"COALESCE({_number(_V, 'filesize')} * 8 / 1000 / NULLIF(duration, 0),"
    f" NULLIF({_number(_V, 'vbr')}, 0), {_number(_V, 'tbr')})::double precision"
)
_OLD_KBPS = f"COALESCE(NULLIF({_number(_V, 'vbr')}, 0), {_number(_V, 'tbr')})::double precision"


def upgrade() -> None:
    # A recording's info dict has no duration. The format watch measured it.
    op.execute(
        "UPDATE video SET duration = round(w.stored_duration) FROM format_watch w"
        " WHERE w.video_id = video.id AND video.duration IS NULL"
        " AND w.stored_duration IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE video DROP COLUMN video_kbps,"
        f" ADD COLUMN video_kbps double precision GENERATED ALWAYS AS ({_KBPS}) STORED,"
        f" ADD COLUMN video_protocol text GENERATED ALWAYS AS ({_V}->>'protocol') STORED"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE video DROP COLUMN video_protocol, DROP COLUMN video_kbps,"
        f" ADD COLUMN video_kbps double precision GENERATED ALWAYS AS ({_OLD_KBPS}) STORED"
    )
