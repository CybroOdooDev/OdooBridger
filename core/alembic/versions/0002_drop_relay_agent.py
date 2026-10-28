"""Drop what the OdooBridger Agent left behind (removed Oct 27, 2026).

The ``relay_agent`` table and ``odoo_connection.relay_agent_id`` (with its
index and foreign key) exist only in databases made before the agent was
removed; nothing has read them since. A fresh database never had them, so
every step checks first.

Revision ID: 0002_drop_relay_agent
Revises: 0001_baseline
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_drop_relay_agent"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from obcore.migrations import drop_agent_leftovers

    drop_agent_leftovers(op)


def downgrade() -> None:
    # The agent is gone from the code; there is nothing to bring back.
    pass
