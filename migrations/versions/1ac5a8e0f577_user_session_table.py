"""user session table

Revision ID: 1ac5a8e0f577
Revises: 38a330686a17
Create Date: 2024-11-23 11:25:01.723169

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = "1ac5a8e0f577"
down_revision = "38a330686a17"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_session",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("room_name", sa.String(length=128), nullable=True),
        sa.Column("room_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )


def downgrade():
    op.drop_table("user_session")
