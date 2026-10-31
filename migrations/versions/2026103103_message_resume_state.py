"""a reply waiting on a delegate keeps where to resume it after a restart:
message.resume_state

uncloseai-cli runs on in its sandbox when our server restarts (a deploy);
only our watching loop died, so the reply showed "interrupted" & the run's
result was lost (fox, 2026-10-31). The checkpoint lets a restarted server
reattach to the run & carry the reply on.

Revision ID: 2026103103
Revises: 2026103102
Create Date: 2026-10-31 18:30:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026103103"
down_revision = "2026103102"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.add_column(sa.Column("resume_state", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("message", schema=None) as batch_op:
        batch_op.drop_column("resume_state")
