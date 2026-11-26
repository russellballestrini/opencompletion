"""message.created_at: when a message was written

Unix seconds, set when its row is made (a person's message as it is sent,
a reply as it starts), so our page shows a timeline. Rows from before
this migration have none & show no time: nothing is backfilled.

Revision ID: 2026112501
Revises: 2026112101
Create Date: 2026-11-25 18:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026112501"
down_revision = "2026112101"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column("created_at", sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table("message") as batch_op:
        batch_op.drop_column("created_at")
