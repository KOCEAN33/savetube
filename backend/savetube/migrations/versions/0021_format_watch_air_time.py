"""format watch runs from the live's air time

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-24 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
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
_OLD = f"({_AIRED} IS NULL OR {_AIRED} <= now() - interval '7 days')"


def upgrade() -> None:
    op.execute(
        f"UPDATE format_watch w SET started_at = {_AIRED} FROM video v"
        f" WHERE v.id = w.video_id AND {_AIRED} IS NOT NULL"
    )
    # Cancel the queued replaces of old watches, and the automatic ones of
    # recent watches: the next probe queues again what still ranks above the
    # stored stream. A manual pick on a recent watch waits. A replace whose
    # queue job a worker already took is left to run.
    queue = op.get_bind().scalar(sa.text("SELECT to_regclass('procrastinate_jobs')"))
    taken = (
        " AND (j.queue_job_id IS NULL OR j.queue_job_id NOT IN"
        " (SELECT id FROM procrastinate_jobs WHERE status <> 'todo'))"
        if queue is not None
        else ""
    )
    op.execute(
        "UPDATE job j SET state = 'cancelled', finished_at = now()"
        " FROM format_watch w JOIN video v ON v.id = w.video_id"
        " WHERE j.video_id = w.video_id AND j.kind = 'replace' AND j.state = 'queued'"
        f" AND (NOT w.manual OR {_OLD}){taken}"
    )
    if queue is not None:
        op.execute(
            "DELETE FROM procrastinate_jobs WHERE status = 'todo' AND id IN"
            " (SELECT queue_job_id FROM job WHERE kind = 'replace' AND state = 'cancelled')"
        )
    # A watch with an original moved aside stays open, so the scheduler ends
    # it and deletes that file. A migration deletes no file.
    op.execute(
        "UPDATE format_watch w SET ended_at = now() FROM video v"
        " WHERE v.id = w.video_id AND w.ended_at IS NULL AND w.original_path IS NULL"
        f" AND {_OLD}"
        " AND NOT EXISTS (SELECT 1 FROM job j WHERE j.video_id = w.video_id"
        " AND j.kind = 'replace' AND j.state IN ('queued', 'running'))"
    )


def downgrade() -> None:
    pass
