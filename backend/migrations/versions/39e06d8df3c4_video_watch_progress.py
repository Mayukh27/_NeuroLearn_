"""video watch progress (per-student resume position)

Revision ID: 39e06d8df3c4
Revises: 313f4638aea9
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '39e06d8df3c4'
down_revision: Union[str, Sequence[str], None] = '313f4638aea9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'video_watch_progress',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('user_id', sa.String(), nullable=False),
        sa.Column('video_id', sa.String(), nullable=False),
        sa.Column('position_seconds', sa.Float(), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['video_id'], ['local_videos.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'video_id', name='uq_video_watch_progress_user_video'),
    )
    op.create_index(
        op.f('ix_video_watch_progress_user_id'), 'video_watch_progress', ['user_id'], unique=False
    )
    op.create_index(
        op.f('ix_video_watch_progress_video_id'), 'video_watch_progress', ['video_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_video_watch_progress_video_id'), table_name='video_watch_progress')
    op.drop_index(op.f('ix_video_watch_progress_user_id'), table_name='video_watch_progress')
    op.drop_table('video_watch_progress')
