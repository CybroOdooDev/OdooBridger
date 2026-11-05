"""Overtime Odoo split off a shift, shown on the shift instead of as its own record.

Odoo 20's time rules cut a long shift into an attendance record and an overtime
record. BioBridge keeps the shift whole; this is how much of it Odoo counts as
overtime, for the "A + OT" remark on the Attendance page.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-05 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0018'
down_revision: Union[str, None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('attendance_record') as batch:
        batch.add_column(sa.Column('overtime_hours', sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('attendance_record') as batch:
        batch.drop_column('overtime_hours')
