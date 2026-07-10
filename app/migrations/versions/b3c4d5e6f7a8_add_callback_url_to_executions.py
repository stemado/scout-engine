"""add callback_url to executions

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-13 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str]] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add callback_url column to executions table."""
    op.add_column('executions', sa.Column('callback_url', sa.String(2048), nullable=True))


def downgrade() -> None:
    """Remove callback_url column from executions table."""
    op.drop_column('executions', 'callback_url')
