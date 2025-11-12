"""add_updated_at_to_room

Revision ID: 5d0d533ff7c0
Revises: 2025011100
Create Date: 2025-11-11 21:53:13.141580

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5d0d533ff7c0'
down_revision = '2025011100'
branch_labels = None
depends_on = None


def upgrade():
    # Add updated_at column to room table (Unix timestamp as integer)
    with op.batch_alter_table('room', schema=None) as batch_op:
        batch_op.add_column(sa.Column('updated_at', sa.Integer(), nullable=False, server_default=sa.text('(strftime(\'%s\', \'now\'))')))


def downgrade():
    # Remove updated_at column from room table
    with op.batch_alter_table('room', schema=None) as batch_op:
        batch_op.drop_column('updated_at')
