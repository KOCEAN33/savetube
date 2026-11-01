"""videos the user deleted, kept as rows so nothing downloads them again

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-31 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0027'
down_revision: str | None = '0026'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'pending', 'downloading', 'complete', 'failed', 'staged'"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES}, 'deleted')")


def downgrade() -> None:
    # Complete is the one old state the poll never queues, so the old code
    # downloads no deleted video again. Its files stay gone and dir_path null.
    op.execute("UPDATE video SET state = 'complete' WHERE state = 'deleted'")
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES})")
