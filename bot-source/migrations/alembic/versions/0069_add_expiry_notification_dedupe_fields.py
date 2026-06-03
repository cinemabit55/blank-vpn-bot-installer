"""add expiry notification dedupe fields

Revision ID: 0069
Revises: 0068
Create Date: 2026-05-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0069'
down_revision: Union[str, None] = '0068'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLE_NAME = 'sent_notifications'
UNIQUE_NAME = 'uq_sent_notifications_subscription_expiry'
UNIQUE_COLUMNS = ['subscription_id', 'notification_type', 'minutes_before', 'expires_at']


def _columns() -> set[str]:
    return {column['name'] for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)}


def _has_unique() -> bool:
    inspector = sa.inspect(op.get_bind())

    for constraint in inspector.get_unique_constraints(TABLE_NAME):
        if constraint.get('name') == UNIQUE_NAME:
            return True
        if constraint.get('column_names') == UNIQUE_COLUMNS:
            return True

    for index in inspector.get_indexes(TABLE_NAME):
        if index.get('name') == UNIQUE_NAME:
            return True
        if index.get('unique') and index.get('column_names') == UNIQUE_COLUMNS:
            return True

    return False


def _has_unique_name() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(constraint.get('name') == UNIQUE_NAME for constraint in inspector.get_unique_constraints(TABLE_NAME))


def upgrade() -> None:
    columns = _columns()

    if 'minutes_before' not in columns:
        op.add_column(TABLE_NAME, sa.Column('minutes_before', sa.Integer(), nullable=True))

    if 'expires_at' not in columns:
        op.add_column(TABLE_NAME, sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))

    if not _has_unique():
        op.create_unique_constraint(UNIQUE_NAME, TABLE_NAME, UNIQUE_COLUMNS)


def downgrade() -> None:
    columns = _columns()

    if _has_unique_name():
        op.drop_constraint(UNIQUE_NAME, TABLE_NAME, type_='unique')

    if 'expires_at' in columns:
        op.drop_column(TABLE_NAME, 'expires_at')

    if 'minutes_before' in columns:
        op.drop_column(TABLE_NAME, 'minutes_before')
