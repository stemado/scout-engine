"""add workflow_fragments table

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-02 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str]] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create workflow_fragments table."""
    op.create_table(
        'workflow_fragments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('fragment_id', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('variables', JSONB, nullable=True),
        sa.Column('steps', JSONB, nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_workflow_fragments_fragment_id', 'workflow_fragments', ['fragment_id'], unique=True)


def downgrade() -> None:
    """Drop workflow_fragments table."""
    op.drop_index('ix_workflow_fragments_fragment_id', table_name='workflow_fragments')
    op.drop_table('workflow_fragments')
