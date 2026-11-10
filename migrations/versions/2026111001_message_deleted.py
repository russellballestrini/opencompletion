"""a deleted message stays as a row & can be restored: message.deleted_at,
message.deleted_by

In a public chatroom anyone may delete any message. Its chain said who
did, but the row was gone, so vandalism could be named & never undone.
Delete now marks the row (when, & the name that did it); every reader
skips marked rows, & Restore clears the marks (chain events `delete`,
then `restore`). Only deleting a room removes its rows.

Revision ID: 2026111001
Revises: 2026110201
Create Date: 2026-11-10 21:45:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026111001"
down_revision = "2026110201"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("deleted_by", sa.String(length=128), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_column("deleted_by")
        batch_op.drop_column("deleted_at")
