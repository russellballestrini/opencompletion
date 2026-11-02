"""room_event & room_checkpoint: a room's append-only, hash-chained history

Each event in a room (a person's message, a tool call's result, a
model's reply, an edit, a fork, a deletion) is one unfirehose-chain-v1
line, hash-linked to the one before, with merkle-v1 roots as checkpoints
(chain.py, vendored provenance.py). A model's tool results become
evidence next to what it claimed (fox, 2026-11-02: a model said a 404
repo was live). No foreign key to room: the chain outlives its room.

Revision ID: 2026110201
Revises: 2026103104
Create Date: 2026-11-02 21:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026110201"
down_revision = "2026103104"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "room_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("room_name", sa.String(length=128), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("line", sa.UnicodeText(), nullable=False),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("chain", "seq", name="uq_room_event_seq"),
    )
    op.create_index("ix_room_event_chain", "room_event", ["chain"])
    op.create_index("ix_room_event_room_id", "room_event", ["room_id"])
    op.create_index("ix_room_event_message_id", "room_event", ["message_id"])
    op.create_index("ix_room_event_hash", "room_event", ["hash"])
    op.create_table(
        "room_checkpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chain", sa.String(length=64), nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("upto_seq", sa.Integer(), nullable=False),
        sa.Column("root", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_room_checkpoint_chain", "room_checkpoint", ["chain"])
    op.create_index("ix_room_checkpoint_room_id", "room_checkpoint", ["room_id"])


def downgrade():
    op.drop_index("ix_room_checkpoint_room_id", table_name="room_checkpoint")
    op.drop_index("ix_room_checkpoint_chain", table_name="room_checkpoint")
    op.drop_table("room_checkpoint")
    op.drop_index("ix_room_event_hash", table_name="room_event")
    op.drop_index("ix_room_event_message_id", table_name="room_event")
    op.drop_index("ix_room_event_room_id", table_name="room_event")
    op.drop_index("ix_room_event_chain", table_name="room_event")
    op.drop_table("room_event")
