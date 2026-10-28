"""Placeholder (was: relay_agent_id column, removed with the OdooBridger Agent)

The revision stays in the chain because databases may already be stamped at it.
A column added by an earlier version of this migration is simply left unused.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23 12:00:00
"""
from typing import Sequence, Union


revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
