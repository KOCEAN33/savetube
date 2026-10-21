"""archive jobs wait for the dispatcher

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A database migrated without `savetube migrate` has no procrastinate schema.
    queue = op.get_bind().scalar(sa.text("SELECT to_regclass('procrastinate_jobs')"))
    if queue is None:
        op.execute(
            "UPDATE job SET queue_job_id = NULL, deferred_until = NULL"
            " WHERE kind = 'archive' AND state = 'queued'"
        )
        return
    # Every slot or window deferral left one of these behind.
    op.execute("DELETE FROM procrastinate_jobs WHERE status = 'succeeded'")
    # A waiting archive job now has no queue job. The scheduler's dispatcher
    # queues it. A `doing` row is a running task and stays.
    op.execute("DELETE FROM procrastinate_jobs WHERE status = 'todo' AND task_name = 'archive'")
    op.execute(
        """
        UPDATE job SET queue_job_id = NULL, deferred_until = NULL
        WHERE kind = 'archive' AND state = 'queued'
          AND (queue_job_id IS NULL OR queue_job_id NOT IN (
              SELECT id FROM procrastinate_jobs WHERE status = 'doing'))
        """
    )


def downgrade() -> None:
    # The old code queues an archive job only from a poll, and a poll skips a
    # video with a queued job. A cancelled job lets the next poll queue it again.
    op.execute(
        """
        UPDATE job SET state = 'cancelled', finished_at = now()
        WHERE kind = 'archive' AND state = 'queued' AND queue_job_id IS NULL
        """
    )
