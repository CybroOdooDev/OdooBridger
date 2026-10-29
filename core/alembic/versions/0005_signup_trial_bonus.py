"""Free trial days for accounts that paid at registration.

Revision ID: 0005_signup_trial_bonus
Revises: 0004_signup_pay_first
Create Date: 2026-10-29 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005_signup_trial_bonus'
down_revision: Union[str, None] = '0004_signup_pay_first'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paid_at_signup', sa.Boolean(), server_default=sa.text('false'), nullable=False))
        batch_op.add_column(sa.Column('trial_bonus_days', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('trial_bonus_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_column('trial_bonus_at')
        batch_op.drop_column('trial_bonus_days')
        batch_op.drop_column('paid_at_signup')
