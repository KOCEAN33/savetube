"""interrupted jobs, resumable and verified downloads

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'queued', 'running', 'succeeded', 'failed', 'missed', 'cancelled'"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_job_jobstate'), 'job', type_='check')
    op.create_check_constraint(op.f('ck_job_jobstate'), 'job', f"state IN ({_STATES}, 'interrupted')")
    op.add_column('video', sa.Column('resume_format', sa.Text(), nullable=True))
    op.add_column('video', sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('video', sa.Column('verified_checks', postgresql.ARRAY(sa.Text()), nullable=True))


def downgrade() -> None:
    # The old code counts a failed job as an attempt, and an interrupted run is none.
    op.execute("UPDATE job SET state = 'cancelled' WHERE state = 'interrupted'")
    op.drop_constraint(op.f('ck_job_jobstate'), 'job', type_='check')
    op.create_check_constraint(op.f('ck_job_jobstate'), 'job', f"state IN ({_STATES})")
    op.drop_column('video', 'verified_checks')
    op.drop_column('video', 'verified_at')
    op.drop_column('video', 'resume_format')
