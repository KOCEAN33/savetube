"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-20 13:33:21.393339
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('channel',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('handle', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('dir_name', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_channel')),
    sa.UniqueConstraint('dir_name', name=op.f('uq_channel_dir_name'))
    )
    op.create_table('rule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('types', postgresql.ARRAY(sa.String(length=16)), nullable=False),
    sa.Column('date_after', sa.Date(), nullable=True),
    sa.Column('format', sa.String(), nullable=False),
    sa.Column('use_cookies', sa.Boolean(), nullable=False),
    sa.Column('watch_lives', sa.Boolean(), nullable=False),
    sa.Column('max_concurrent', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rule')),
    sa.UniqueConstraint('name', name=op.f('uq_rule_name'))
    )
    op.create_table('storage_sample',
    sa.Column('sampled_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('total_bytes', sa.BigInteger(), nullable=False),
    sa.Column('free_bytes', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('sampled_at', name=op.f('pk_storage_sample'))
    )
    op.create_table('source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('url', sa.String(), nullable=False),
    sa.Column('kind', sa.Enum('tab', 'playlist', name='sourcekind', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('channel_id', sa.String(length=64), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('poll_interval_minutes', sa.Integer(), nullable=False),
    sa.Column('rule_id', sa.Integer(), nullable=False),
    sa.Column('last_polled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('tab', 'playlist')", name=op.f('ck_source_sourcekind')),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_source_channel_id_channel')),
    sa.ForeignKeyConstraint(['rule_id'], ['rule.id'], name=op.f('fk_source_rule_id_rule')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_source')),
    sa.UniqueConstraint('url', name=op.f('uq_source_url'))
    )
    op.create_table('video',
    sa.Column('id', sa.String(length=16), nullable=False),
    sa.Column('channel_id', sa.String(length=64), nullable=False),
    sa.Column('type', sa.Enum('videos', 'shorts', 'lives', 'members', name='videotype', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('upload_date', sa.Date(), nullable=True),
    sa.Column('duration', sa.Integer(), nullable=True),
    sa.Column('live_status', sa.String(), nullable=True),
    sa.Column('availability', sa.String(), nullable=True),
    sa.Column('format_id', sa.String(), nullable=True),
    sa.Column('dir_path', sa.String(), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('state', sa.Enum('pending', 'downloading', 'complete', 'failed', name='videostate', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('info', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'downloading', 'complete', 'failed')", name=op.f('ck_video_videostate')),
    sa.CheckConstraint("type IN ('videos', 'shorts', 'lives', 'members')", name=op.f('ck_video_videotype')),
    sa.ForeignKeyConstraint(['channel_id'], ['channel.id'], name=op.f('fk_video_channel_id_channel')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_video'))
    )
    op.create_index(op.f('ix_video_channel_id'), 'video', ['channel_id'], unique=False)
    op.create_index(op.f('ix_video_completed_at'), 'video', ['completed_at'], unique=False)
    op.create_table('job',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('archive', 'record', name='jobkind', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('state', sa.Enum('queued', 'running', 'succeeded', 'failed', 'missed', 'cancelled', name='jobstate', native_enum=False, create_constraint=False, length=32), nullable=False),
    sa.Column('video_id', sa.String(length=16), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('miss_reason', sa.Enum('dvr_off', 'ended_before_detection', 'recorder_failed', name='missreason', native_enum=False, create_constraint=False, length=32), nullable=True),
    sa.Column('log_tail', sa.Text(), nullable=True),
    sa.Column('k8s_job_name', sa.String(), nullable=True),
    sa.Column('queue_job_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('archive', 'record')", name=op.f('ck_job_jobkind')),
    sa.CheckConstraint("miss_reason IN ('dvr_off', 'ended_before_detection', 'recorder_failed')", name=op.f('ck_job_missreason')),
    sa.CheckConstraint("state IN ('queued', 'running', 'succeeded', 'failed', 'missed', 'cancelled')", name=op.f('ck_job_jobstate')),
    sa.ForeignKeyConstraint(['video_id'], ['video.id'], name=op.f('fk_job_video_id_video')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job'))
    )
    op.create_index(op.f('ix_job_state'), 'job', ['state'], unique=False)
    op.create_index(op.f('ix_job_video_id'), 'job', ['video_id'], unique=False)
    op.create_table('video_source',
    sa.Column('video_id', sa.String(length=16), nullable=False),
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], name=op.f('fk_video_source_source_id_source'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['video_id'], ['video.id'], name=op.f('fk_video_source_video_id_video'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('video_id', 'source_id', name=op.f('pk_video_source'))
    )


def downgrade() -> None:
    op.drop_table('video_source')
    op.drop_index(op.f('ix_job_video_id'), table_name='job')
    op.drop_index(op.f('ix_job_state'), table_name='job')
    op.drop_table('job')
    op.drop_index(op.f('ix_video_completed_at'), table_name='video')
    op.drop_index(op.f('ix_video_channel_id'), table_name='video')
    op.drop_table('video')
    op.drop_table('source')
    op.drop_table('storage_sample')
    op.drop_table('rule')
    op.drop_table('channel')
