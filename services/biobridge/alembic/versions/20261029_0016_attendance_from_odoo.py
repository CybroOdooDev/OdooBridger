"""Follow Odoo's attendance: where a record came from, and how far the copy is current.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-29 12:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('attendance_record') as batch:
        batch.add_column(sa.Column('origin', sa.String(length=10), server_default=sa.text("'biobridge'"),
                                   nullable=False))
    with op.batch_alter_table('tenant') as batch:
        batch.add_column(sa.Column('odoo_attendance_synced_at', sa.DateTime(timezone=False), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('tenant') as batch:
        batch.drop_column('odoo_attendance_synced_at')
    with op.batch_alter_table('attendance_record') as batch:
        batch.drop_column('origin')
