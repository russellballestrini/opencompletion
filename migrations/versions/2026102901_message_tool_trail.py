"""keep what a reply's tools did: message.tool_trail

Revision ID: 2026102901
Revises: 2026102801
Create Date: 2026-10-29 00:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2026102901"
down_revision = "2026102801"
branch_labels = None
depends_on = None


def upgrade():
    # Skip what create_all() (init_db.py, app startup) already made.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "tool_trail" not in columns:
        with op.batch_alter_table("message") as batch_op:
            batch_op.add_column(sa.Column("tool_trail", sa.Text()))


def downgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("tool_trail")
