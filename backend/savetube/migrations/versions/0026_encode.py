"""encode_settings and encode_job: external re-encoding (docs/plan/encode.md)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-31 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0026'
down_revision: str | None = '0025'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('encode_settings',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column('profile', sa.Text(), server_default='vtuber-av1-cq46', nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_encode_settings_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_encode_settings'))
    )
    op.execute("INSERT INTO encode_settings (id) VALUES (1)")
    op.create_table('encode_job',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('video_id', sa.String(length=16), nullable=False),
    sa.Column('state', sa.Enum('queued', 'claimed', 'succeeded', 'failed', 'cancelled', 'requeued', 'lost', name='encodestate', native_enum=False, create_constraint=True, length=32), nullable=False),
    sa.Column('profile', sa.Text(), nullable=False),
    sa.Column('source_path', sa.Text(), nullable=False),
    sa.Column('source_size', sa.BigInteger(), nullable=False),
    sa.Column('encoder', sa.Text(), nullable=True),
    sa.Column('ffmpeg_version', sa.Text(), nullable=True),
    sa.Column('result_size', sa.BigInteger(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('log_tail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['video_id'], ['video.id'], name=op.f('fk_encode_job_video_id_video')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_encode_job'))
    )
    op.create_index(op.f('ix_encode_job_state'), 'encode_job', ['state'], unique=False)
    op.create_index(op.f('ix_encode_job_video_id'), 'encode_job', ['video_id'], unique=False)
    op.create_index('uq_encode_job_open_video', 'encode_job', ['video_id'], unique=True, postgresql_where=sa.text("state IN ('queued', 'claimed')"))


def downgrade() -> None:
    op.drop_table('encode_job')
    op.drop_table('encode_settings')
