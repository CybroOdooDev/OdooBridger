"""Pay-first signups: the pending signup carries its Stripe checkout.

For "Buy Now" the visitor pays first and only then gets the confirmation link,
so the pending signup has to remember the checkout and, once paid, the
subscription that the new account takes over.

Revision ID: 0004_signup_pay_first
Revises: 0003_closure_created_at
Create Date: 2026-10-29 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004_signup_pay_first'
down_revision: Union[str, None] = '0003_closure_created_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('pending_signup', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stripe_checkout_id', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('stripe_customer_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('stripe_subscription_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('pending_signup', schema=None) as batch_op:
        batch_op.drop_column('paid_at')
        batch_op.drop_column('stripe_subscription_id')
        batch_op.drop_column('stripe_customer_id')
        batch_op.drop_column('stripe_checkout_id')
