"""A suggested Odoo employee on a badge that needs a person to decide.

Same badge but a different name, or the same name with a different (or no)
badge: the badge is not matched on its own; the likely Odoo employee is kept
here for the person who confirms or corrects it.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-30 10:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.add_column(sa.Column('suggested_employee_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('suggested_employee_name', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('suggestion_kind', sa.String(length=24), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('employee_mapping') as batch:
        batch.drop_column('suggestion_kind')
        batch.drop_column('suggested_employee_name')
        batch.drop_column('suggested_employee_id')
