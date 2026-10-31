"""a chat's delegates share one uncloseai-cli session: sandbox.delegate_session_id

Each delegate started a fresh uncloseai-cli session, so a second task in
the same chat forgot what the first learned (fox, 2026-10-31: resume by
default). The sandbox keeps the session id its delegates resume.

Revision ID: 2026103104
Revises: 2026103103
Create Date: 2026-10-31 20:30:00

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026103104"
down_revision = "2026103103"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sandbox", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("delegate_session_id", sa.String(length=128), nullable=True)
        )


def downgrade():
    with op.batch_alter_table("sandbox", schema=None) as batch_op:
        batch_op.drop_column("delegate_session_id")
