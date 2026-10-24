"""stored stream quality as generated columns of video.info

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-24 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0022'
down_revision: str | None = '0021'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# savetube.formats.stored_streams in SQL: the first requested format with that
# stream, or the info dict itself without requested formats.
def _stream(codec: str) -> str:
    return (
        "(CASE WHEN info->'requested_formats'->0 IS NULL THEN info"
        " ELSE jsonb_path_query_first(info,"
        f" '$.requested_formats[*] ? (@.{codec} != null && @.{codec} != \"none\")') END)"
    )


def _number(stream: str, key: str) -> str:
    return f"jsonb_path_query_first({stream}, '$.{key} ? (@.type() == \"number\")')::numeric"


_V = _stream("vcodec")
_A = _stream("acodec")
_COLUMNS = {
    'video_codec': (sa.Text(), f"NULLIF({_V}->>'vcodec', 'none')"),
    'audio_codec': (sa.Text(), f"NULLIF({_A}->>'acodec', 'none')"),
    'video_width': (sa.Integer(), f"round({_number(_V, 'width')})::integer"),
    'video_height': (sa.Integer(), f"round({_number(_V, 'height')})::integer"),
    'video_fps': (sa.Float(), f"{_number(_V, 'fps')}::double precision"),
    'video_kbps': (
        sa.Float(),
        f"COALESCE(NULLIF({_number(_V, 'vbr')}, 0), {_number(_V, 'tbr')})::double precision",
    ),
    'premium': (sa.Boolean(), f"COALESCE({_V}->>'format_note' LIKE '%Premium%', false)"),
}


def upgrade() -> None:
    # One rewrite of the table for all columns. The Videos page filters on
    # them, and reading them from the TOASTed info took seconds per query on
    # 5,000 videos.
    op.execute(
        "ALTER TABLE video "
        + ", ".join(
            f"ADD COLUMN {name} {type_.compile(dialect=op.get_bind().dialect)}"
            f" GENERATED ALWAYS AS ({expr}) STORED"
            for name, (type_, expr) in _COLUMNS.items()
        )
    )


def downgrade() -> None:
    op.execute("ALTER TABLE video " + ", ".join(f"DROP COLUMN {name}" for name in _COLUMNS))
