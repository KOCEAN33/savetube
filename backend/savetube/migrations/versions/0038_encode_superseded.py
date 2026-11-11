"""encode_job state 'superseded': a replace swapped out the video stream an
encode made, so the video may be encoded again (docs/plan/encode.md, Which videos)

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-11 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0038'
down_revision: str | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'queued', 'claimed', 'succeeded', 'failed', 'cancelled', 'requeued', 'lost'"


def upgrade() -> None:
    op.drop_constraint(op.f('ck_encode_job_encodestate'), 'encode_job', type_='check')
    op.create_check_constraint(
        op.f('ck_encode_job_encodestate'), 'encode_job', f"state IN ({_STATES}, 'superseded')"
    )
    # Every encode stores AV1, so a video stream of another codec came from a
    # later replace.
    op.execute("""
        UPDATE encode_job e SET state = 'superseded'
        FROM video v
        WHERE v.id = e.video_id AND e.state = 'succeeded'
          AND v.video_codec IS NOT NULL AND lower(v.video_codec) NOT LIKE 'av01%'
    """)


def downgrade() -> None:
    op.execute("UPDATE encode_job SET state = 'succeeded' WHERE state = 'superseded'")
    op.drop_constraint(op.f('ck_encode_job_encodestate'), 'encode_job', type_='check')
    op.create_check_constraint(op.f('ck_encode_job_encodestate'), 'encode_job', f"state IN ({_STATES})")
