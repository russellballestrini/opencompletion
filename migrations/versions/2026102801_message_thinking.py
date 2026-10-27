"""keep a model's thinking: message.thinking

Revision ID: 2026102801
Revises: 2026102702
Create Date: 2026-10-28 00:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "2026102801"
down_revision = "2026102702"
branch_labels = None
depends_on = None


def upgrade():
    # Skip what create_all() (init_db.py, app startup) already made.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("message")}
    if "thinking" not in columns:
        with op.batch_alter_table("message") as batch_op:
            batch_op.add_column(sa.Column("thinking", sa.Text()))


def downgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("thinking")
