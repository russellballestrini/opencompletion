"""a chat's unsandbox sandbox: table sandbox

Revision ID: 2026102902
Revises: 2026102901
Create Date: 2026-10-29 17:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026102902"
down_revision = "2026102901"
branch_labels = None
depends_on = None


def upgrade():
    # Skip what create_all() (init_db.py, app startup) already made.
    inspector = sa.inspect(op.get_bind())
    if "sandbox" not in set(inspector.get_table_names()):
        op.create_table(
            "sandbox",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "room_id",
                sa.Integer(),
                sa.ForeignKey("room.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("service_id", sa.String(length=128), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("token_sha256", sa.String(length=64)),
            sa.Column("model", sa.String(length=255)),
            sa.Column("created_by", sa.String(length=255)),
            sa.Column("last_used_at", sa.Float()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    indexes = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("sandbox")}
    if "ix_sandbox_token_sha256" not in indexes:
        op.create_index("ix_sandbox_token_sha256", "sandbox", ["token_sha256"])


def downgrade():
    op.drop_index("ix_sandbox_token_sha256", table_name="sandbox")
    op.drop_table("sandbox")
