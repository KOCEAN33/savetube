"""format watch of stored lives, and replace jobs

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-24 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0020'
down_revision: str | None = '0019'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = "'archive', 'record'"


def upgrade() -> None:
    op.create_table(
        'format_watch',
        sa.Column('video_id', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('formats', postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True),
        sa.Column('vod_duration', sa.Float(), nullable=True),
        sa.Column('stored_duration', sa.Float(), nullable=True),
        sa.Column('probe_error', sa.Text(), nullable=True),
        sa.Column('manual', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('original_path', sa.Text(), nullable=True),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['video_id'], ['video.id'], name=op.f('fk_format_watch_video_id_video'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('video_id', name=op.f('pk_format_watch')),
    )
    op.add_column('job', sa.Column('format_id', sa.Text(), nullable=True))
    op.drop_constraint(op.f('ck_job_jobkind'), 'job', type_='check')
    op.create_check_constraint(op.f('ck_job_jobkind'), 'job', f"kind IN ({_KINDS}, 'replace')")
    # Lives stored in the last 7 days get the rest of their watch.
    op.execute(
        "INSERT INTO format_watch (video_id, started_at)"
        " SELECT id, completed_at FROM video"
        " WHERE state = 'complete' AND completed_at > now() - interval '7 days'"
        " AND live_status IN ('is_live', 'was_live', 'post_live')"
    )


def downgrade() -> None:
    # Files a replace moved aside stay in `.originals/` on the archive.
    op.execute("DELETE FROM job WHERE kind = 'replace'")
    op.drop_constraint(op.f('ck_job_jobkind'), 'job', type_='check')
    op.create_check_constraint(op.f('ck_job_jobkind'), 'job', f"kind IN ({_KINDS})")
    op.drop_column('job', 'format_id')
    op.drop_table('format_watch')
