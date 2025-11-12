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
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_private", sa.Boolean(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()))
        batch_op.add_column(sa.Column("forked_from_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_room_is_private"), ["is_private"], unique=False)
        batch_op.create_index(batch_op.f("ix_room_is_archived"), ["is_archived"], unique=False)
        batch_op.create_index(batch_op.f("ix_room_owner_id"), ["owner_id"], unique=False)
        batch_op.create_foreign_key("fk_room_owner_id", "user", ["owner_id"], ["id"])
        batch_op.create_foreign_key("fk_room_forked_from_id", "room", ["forked_from_id"], ["id"])


def downgrade():
    pass
