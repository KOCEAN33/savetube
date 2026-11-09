"""videos YouTube no longer serves, kept as rows so nothing downloads them again

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-09 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = '0031'
down_revision: str | None = '0030'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATES = "'pending', 'downloading', 'complete', 'failed', 'staged', 'deleted'"
# savetube.ytdl.PERMANENT_MARKERS as of this revision. A bare "Video
# unavailable" needs a probe beside it, so only a failure retried by hand
# marks such a video.
_GONE = (
    "private video",
    "has been removed",
    "account associated with this video has been terminated",
    "copyright claim",
)


def upgrade() -> None:
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES}, 'gone')")
    said_gone = " OR ".join(f"lower(j.log_tail) LIKE '%{m}%'" for m in _GONE)
    op.execute(f"""
        UPDATE video v SET state = 'gone'
        WHERE v.state = 'failed' AND EXISTS (
            SELECT 1 FROM job j
            WHERE j.video_id = v.id AND j.kind = 'archive' AND j.state = 'failed'
              AND ({said_gone})
              AND NOT EXISTS (SELECT 1 FROM job l WHERE l.video_id = v.id AND l.id > j.id)
        )
    """)


def downgrade() -> None:
    op.execute("UPDATE video SET state = 'failed' WHERE state = 'gone'")
    op.drop_constraint(op.f('ck_video_videostate'), 'video', type_='check')
    op.create_check_constraint(op.f('ck_video_videostate'), 'video', f"state IN ({_STATES})")
