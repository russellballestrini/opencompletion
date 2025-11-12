"""Add authentication system with User, OTPToken models and Room ownership fields

Revision ID: 2025011100
Revises: 5d93cdf18549
Create Date: 2025-01-11 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = "2025011100"
down_revision = "5d93cdf18549"
branch_labels = None
depends_on = None


def upgrade():
    # Add new columns to Room table
    # Note: User and OTPToken tables are created by db.create_all() in make init-db
    # Check if columns exist before adding (in case db.create_all() was run first)

    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col['name'] for col in inspector.get_columns('room')]

    if 'is_private' not in columns:
        op.add_column('room', sa.Column('is_private', sa.Boolean(), nullable=False, server_default='0'))

    if 'is_archived' not in columns:
        op.add_column('room', sa.Column('is_archived', sa.Boolean(), nullable=False, server_default='0'))

    if 'owner_id' not in columns:
        op.add_column('room', sa.Column('owner_id', sa.Integer(), nullable=True))

    if 'created_at' not in columns:
        op.add_column('room', sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')))

    if 'forked_from_id' not in columns:
        op.add_column('room', sa.Column('forked_from_id', sa.Integer(), nullable=True))

    # Create indexes (check if they exist first)
    indexes = [idx['name'] for idx in inspector.get_indexes('room')]

    if 'ix_room_is_private' not in indexes:
        op.create_index(op.f('ix_room_is_private'), 'room', ['is_private'], unique=False)

    if 'ix_room_is_archived' not in indexes:
        op.create_index(op.f('ix_room_is_archived'), 'room', ['is_archived'], unique=False)

    if 'ix_room_owner_id' not in indexes:
        op.create_index(op.f('ix_room_owner_id'), 'room', ['owner_id'], unique=False)


def downgrade():
    pass
