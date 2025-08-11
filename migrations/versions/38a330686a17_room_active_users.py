"""room active users

Revision ID: 38a330686a17
Revises: d737de68d6fa
Create Date: 2024-11-23 09:52:50.824162

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = "38a330686a17"
down_revision = "d737de68d6fa"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.add_column(sa.Column("active_users", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.drop_column("active_users")
