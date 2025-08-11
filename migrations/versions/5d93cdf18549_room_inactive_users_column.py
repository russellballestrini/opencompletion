"""room inactive_users column

Revision ID: 5d93cdf18549
Revises: 1ac5a8e0f577
Create Date: 2024-11-24 14:04:30.488155

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision = "5d93cdf18549"
down_revision = "1ac5a8e0f577"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.add_column(sa.Column("inactive_users", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.drop_column("inactive_users")
