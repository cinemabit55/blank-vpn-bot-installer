"""add pending_bonus_days to users

Revision ID: 0068
Revises: 0067
Create Date: 2026-05-03
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0068'
down_revision: Union[str, None] = '0067'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('pending_bonus_days', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('users', 'pending_bonus_days')
