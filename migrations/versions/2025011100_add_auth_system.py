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
    # Create User table
    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("display_name"),
    )

    # Create indexes for User table
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_email"), ["email"], unique=True)
        batch_op.create_index(batch_op.f("ix_user_display_name"), ["display_name"], unique=True)

    # Create OTPToken table
    op.create_table(
        "otp_token",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("otp_code", sa.String(length=6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create index for OTPToken table
    with op.batch_alter_table("otp_token", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_otp_token_email"), ["email"], unique=False)

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
    # Remove Room table additions
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.drop_constraint("fk_room_forked_from_id", type_="foreignkey")
        batch_op.drop_constraint("fk_room_owner_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_room_owner_id"))
        batch_op.drop_index(batch_op.f("ix_room_is_archived"))
        batch_op.drop_index(batch_op.f("ix_room_is_private"))
        batch_op.drop_column("forked_from_id")
        batch_op.drop_column("created_at")
        batch_op.drop_column("owner_id")
        batch_op.drop_column("is_archived")
        batch_op.drop_column("is_private")

    # Drop OTPToken table
    with op.batch_alter_table("otp_token", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_otp_token_email"))
    op.drop_table("otp_token")

    # Drop User table
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_user_display_name"))
        batch_op.drop_index(batch_op.f("ix_user_email"))
    op.drop_table("user")
