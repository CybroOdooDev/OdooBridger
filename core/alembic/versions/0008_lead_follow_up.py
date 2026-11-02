"""A follow-up date on each lead.

Revision ID: 0008_lead_follow_up
Revises: 0007_staff_dashboard_history
Create Date: 2026-11-02 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008_lead_follow_up'
down_revision: Union[str, None] = '0007_staff_dashboard_history'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.add_column(sa.Column('follow_up_on', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('lead', schema=None) as batch_op:
        batch_op.drop_column('follow_up_on')
