"""add blob & attachment tables for file uploads

Revision ID: 2026102701
Revises: 5d0d533ff7c0
Create Date: 2026-10-27 15:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2026102701"
down_revision = "5d0d533ff7c0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "blob",
        sa.Column("sha256", sa.String(length=64), primary_key=True),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "attachment",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("room.id"), nullable=False),
        sa.Column(
            "sha256", sa.String(length=64), sa.ForeignKey("blob.sha256"), nullable=False
        ),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=127), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128)),
        sa.Column("source_url", sa.String(length=2048)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_attachment_room_id", "attachment", ["room_id"])
    op.create_index("ix_attachment_source_url", "attachment", ["source_url"])


def downgrade():
    op.drop_index("ix_attachment_source_url", table_name="attachment")
    op.drop_index("ix_attachment_room_id", table_name="attachment")
    op.drop_table("attachment")
    op.drop_table("blob")
