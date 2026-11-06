"""When a platform last heard from each terminal.

Separates "the terminal is up" (a heartbeat the platform reports) from "someone
punched" (``last_seen_at``), so a quiet door is not mistaken for a dead one.

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-06 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('device') as batch:
        batch.add_column(sa.Column('last_contact_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('device') as batch:
        batch.drop_column('last_contact_at')
