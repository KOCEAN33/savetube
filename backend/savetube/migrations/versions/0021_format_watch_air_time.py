"""format watch runs from the live's air time

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-24 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0021'
down_revision: str | None = '0020'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# savetube.watch.aired_at in SQL.
_AIRED = (
    "COALESCE(to_timestamp((v.info->>'release_timestamp')::double precision),"
    " to_timestamp((v.info->>'timestamp')::double precision),"
    " v.upload_date::timestamp AT TIME ZONE 'UTC')"
)


def upgrade() -> None:
    op.execute(
        f"UPDATE format_watch w SET started_at = {_AIRED} FROM video v"
        f" WHERE v.id = w.video_id AND {_AIRED} IS NOT NULL"
    )
    # A watch with an original moved aside stays open, so the scheduler ends
    # it and deletes that file. A migration deletes no file.
    op.execute(
        "UPDATE format_watch w SET ended_at = now() FROM video v"
        " WHERE v.id = w.video_id AND w.ended_at IS NULL AND w.original_path IS NULL"
        f" AND ({_AIRED} IS NULL OR {_AIRED} <= now() - interval '7 days')"
        " AND NOT EXISTS (SELECT 1 FROM job j WHERE j.video_id = w.video_id"
        " AND j.kind = 'replace' AND j.state IN ('queued', 'running'))"
    )


def downgrade() -> None:
    pass
