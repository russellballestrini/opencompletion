"""message.content is text, not 1024 characters

It was declared String(1024) & holds whole replies. SQLite never minded (it
ignores a declared length), so there nothing changes: altering a column in
SQLite rebuilds its table, & `message` keeps an id sequence seeded past
every id our chains ever recorded (2026111201), which a rebuild would have
to carry over for no gain. Any other database gets a real TEXT column,
since it would cut or refuse a long reply.

Revision ID: 2026112101
Revises: 2026111701
Create Date: 2026-11-21 12:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026112101"
down_revision = "2026111701"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        return
    op.alter_column(
        "message",
        "content",
        existing_type=sa.String(length=1024),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade():
    # Never back to 1024 characters: that would cut replies we hold.
    pass
