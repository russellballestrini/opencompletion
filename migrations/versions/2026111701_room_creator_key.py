"""a guest's room remembers the browser that made it: room.creator_key

A chatroom started while signed out had no owner, so nobody could ever
archive or delete it, & the first signed-in person to join one became its
owner. Now a guest's session holds a random key & the room keeps its
sha256: that browser may archive or delete the room, & signing in claims
the rooms it made. Joining never claims a room.

Revision ID: 2026111701
Revises: 2026111201
Create Date: 2026-11-17 13:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026111701"
down_revision = "2026111201"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("creator_key", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_room_creator_key"), ["creator_key"], unique=False
        )


def downgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_room_creator_key"))
        batch_op.drop_column("creator_key")
