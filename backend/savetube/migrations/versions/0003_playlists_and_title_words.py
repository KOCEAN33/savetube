"""channel playlists and title words

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('channel', sa.Column('include_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('channel', sa.Column('exclude_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.create_table('global_words',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('include_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('exclude_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.CheckConstraint('id = 1', name=op.f('ck_global_words_single_row')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_global_words'))
    )
    op.execute("INSERT INTO global_words (id) VALUES (1)")
    op.add_column('source', sa.Column('include_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('source', sa.Column('exclude_words', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    op.add_column('source', sa.Column('other_channels', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('source', sa.Column('parent_id', sa.Integer(), nullable=True))
    op.add_column('source', sa.Column('ignored_urls', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False))
    # Sources from before listing-first keep downloading. The API sets the flag
    # off explicitly on every source it adds.
    op.add_column('source', sa.Column('archiving', sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column('job', sa.Column('deferred_until', sa.DateTime(timezone=True), nullable=True))
    op.drop_column('rule', 'max_concurrent')
    op.create_table('download_settings',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('max_concurrent', sa.Integer(), server_default='1', nullable=False),
    sa.Column('rate_limit_bytes', sa.BigInteger(), nullable=True),
    sa.Column('window_start', sa.Time(), nullable=True),
    sa.Column('window_end', sa.Time(), nullable=True),
    sa.CheckConstraint('id = 1', name=op.f('ck_download_settings_single_row')),
    sa.CheckConstraint('max_concurrent BETWEEN 1 AND 10', name=op.f('ck_download_settings_max_concurrent')),
    sa.CheckConstraint('rate_limit_bytes > 0', name=op.f('ck_download_settings_rate_limit_bytes')),
    sa.CheckConstraint('(window_start IS NULL AND window_end IS NULL) OR (window_start IS NOT NULL AND window_end IS NOT NULL AND window_start <> window_end)', name=op.f('ck_download_settings_window')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_download_settings'))
    )
    op.execute("INSERT INTO download_settings (id) VALUES (1)")
    op.create_foreign_key(op.f('fk_source_parent_id_source'), 'source', 'source', ['parent_id'], ['id'], ondelete='CASCADE')
    op.drop_constraint(op.f('ck_source_sourcekind'), 'source', type_='check')
    op.create_check_constraint(op.f('ck_source_sourcekind'), 'source', "kind IN ('tab', 'playlist', 'playlists')")
    op.create_table('listing_entry',
    sa.Column('source_id', sa.Integer(), nullable=False),
    sa.Column('video_id', sa.String(length=16), nullable=False),
    sa.Column('channel_id', sa.String(length=64), nullable=True),
    sa.Column('title', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], name=op.f('fk_listing_entry_source_id_source'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('source_id', 'video_id', name=op.f('pk_listing_entry'))
    )
    op.create_index(op.f('ix_listing_entry_channel_id'), 'listing_entry', ['channel_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_listing_entry_channel_id'), table_name='listing_entry')
    op.drop_table('listing_entry')
    # The old CHECK has no `playlists`. Their children cascade with them.
    op.execute("DELETE FROM source WHERE kind = 'playlists'")
    op.drop_constraint(op.f('ck_source_sourcekind'), 'source', type_='check')
    op.create_check_constraint(op.f('ck_source_sourcekind'), 'source', "kind IN ('tab', 'playlist')")
    op.drop_constraint(op.f('fk_source_parent_id_source'), 'source', type_='foreignkey')
    op.drop_table('download_settings')
    op.add_column('rule', sa.Column('max_concurrent', sa.Integer(), server_default='1', nullable=False))
    op.drop_column('job', 'deferred_until')
    op.drop_column('source', 'archiving')
    op.drop_column('source', 'ignored_urls')
    op.drop_column('source', 'parent_id')
    op.drop_column('source', 'other_channels')
    op.drop_column('source', 'exclude_words')
    op.drop_column('source', 'include_words')
    op.drop_table('global_words')
    op.drop_column('channel', 'exclude_words')
    op.drop_column('channel', 'include_words')
