"""add created_by_key_id to executions

Revision ID: a1b2c3d4e5f6
Revises: f7a2c1d83e54
Create Date: 2026-03-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str]] = 'f7a2c1d83e54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add created_by_key_id column to executions table."""
    op.add_column('executions', sa.Column('created_by_key_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_executions_created_by_key_id',
        'executions', 'api_keys',
        ['created_by_key_id'], ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    """Remove created_by_key_id column from executions table."""
    op.drop_constraint('fk_executions_created_by_key_id', 'executions', type_='foreignkey')
    op.drop_column('executions', 'created_by_key_id')
