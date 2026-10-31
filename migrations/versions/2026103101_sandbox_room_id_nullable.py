"""a deleted chat's sandbox whose destroy failed outlives it: sandbox.room_id
nullable (state destroy_pending, retried by the hourly sweep)

Revision ID: 2026103101
Revises: 2026102902
Create Date: 2026-10-31 07:00:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026103101"
down_revision = "2026102902"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sandbox", schema=None) as batch_op:
        batch_op.alter_column("room_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    # Rows a deleted chat left behind have no room to point at.
    op.execute("DELETE FROM sandbox WHERE room_id IS NULL")
    with op.batch_alter_table("sandbox", schema=None) as batch_op:
        batch_op.alter_column("room_id", existing_type=sa.Integer(), nullable=False)
