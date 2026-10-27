"""keep edited branches: message.superseded_by_id & revision_of_id

Revision ID: 2026102702
Revises: 2026102701
Create Date: 2026-10-27 17:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026102702"
down_revision = "2026102701"
branch_labels = None
depends_on = None


def upgrade():
    # Skip what create_all() (init_db.py, app startup) already made, as in
    # 2026102701.
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("message")}
    indexes = {index["name"] for index in inspector.get_indexes("message")}
    with op.batch_alter_table("message") as batch_op:
        if "superseded_by_id" not in columns:
            batch_op.add_column(sa.Column("superseded_by_id", sa.Integer()))
        if "revision_of_id" not in columns:
            batch_op.add_column(sa.Column("revision_of_id", sa.Integer()))
        if "ix_message_superseded_by_id" not in indexes:
            batch_op.create_index(
                "ix_message_superseded_by_id", ["superseded_by_id"], unique=False
            )


def downgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_index("ix_message_superseded_by_id")
        batch_op.drop_column("revision_of_id")
        batch_op.drop_column("superseded_by_id")
