"""channel settings replace rules

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('channel', sa.Column('date_after', sa.Date(), nullable=True))
    op.add_column('channel', sa.Column('format', sa.String(), server_default='bv*+ba/b', nullable=False))
    op.add_column('channel', sa.Column('use_cookies', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('channel', sa.Column('watch_lives', sa.Boolean(), server_default=sa.false(), nullable=False))
    # Rules of one channel's sources merge so nothing that was kept is lost:
    # either flag on wins, and the earliest cutoff wins, no cutoff being the
    # earliest of all. The format comes from the lowest source id.
    op.execute(
        """
        UPDATE channel c SET
            watch_lives = m.watch_lives,
            use_cookies = m.use_cookies,
            date_after = m.date_after,
            format = m.format
        FROM (
            SELECT s.channel_id,
                   bool_or(r.watch_lives) AS watch_lives,
                   bool_or(r.use_cookies) AS use_cookies,
                   CASE WHEN bool_or(r.date_after IS NULL) THEN NULL
                        ELSE min(r.date_after) END AS date_after,
                   (array_agg(r.format ORDER BY s.id))[1] AS format
            FROM source s JOIN rule r ON r.id = s.rule_id
            WHERE s.channel_id IS NOT NULL
            GROUP BY s.channel_id
        ) m
        WHERE c.id = m.channel_id
        """
    )
    op.drop_constraint(op.f('fk_source_rule_id_rule'), 'source', type_='foreignkey')
    op.drop_column('source', 'rule_id')
    op.drop_table('rule')


def downgrade() -> None:
    op.create_table('rule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('types', postgresql.ARRAY(sa.String(length=16)), nullable=False),
    sa.Column('date_after', sa.Date(), nullable=True),
    sa.Column('format', sa.String(), nullable=False),
    sa.Column('use_cookies', sa.Boolean(), nullable=False),
    sa.Column('watch_lives', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rule')),
    sa.UniqueConstraint('name', name=op.f('uq_rule_name'))
    )
    # One rule per channel that has sources, and one for sources without a channel.
    op.execute(
        """
        INSERT INTO rule (name, types, date_after, format, use_cookies, watch_lives)
        SELECT c.id, '{videos,shorts,lives,members}', c.date_after, c.format,
               c.use_cookies, c.watch_lives
        FROM channel c WHERE EXISTS (SELECT 1 FROM source s WHERE s.channel_id = c.id)
        """
    )
    op.execute(
        "INSERT INTO rule (name, types, format, use_cookies, watch_lives)"
        " VALUES ('default', '{videos,shorts,lives,members}', 'bv*+ba/b', false, false)"
    )
    op.add_column('source', sa.Column('rule_id', sa.Integer(), nullable=True))
    op.execute(
        "UPDATE source s SET rule_id = coalesce("
        " (SELECT r.id FROM rule r WHERE r.name = s.channel_id),"
        " (SELECT r.id FROM rule r WHERE r.name = 'default'))"
    )
    op.alter_column('source', 'rule_id', nullable=False)
    op.create_foreign_key(op.f('fk_source_rule_id_rule'), 'source', 'rule', ['rule_id'], ['id'])
    op.drop_column('channel', 'watch_lives')
    op.drop_column('channel', 'use_cookies')
    op.drop_column('channel', 'format')
    op.drop_column('channel', 'date_after')
