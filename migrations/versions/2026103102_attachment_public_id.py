"""attachments are addressed by a random public_id, not their row id

/attachments/<id>/<name> let anyone walk a public chat's files by counting
up (fox, 2026-10-31). Every row gets a uuid4 hex, & every link to it in a
message or a reply's tool trail is rewritten to carry it.

Revision ID: 2026103102
Revises: 2026103101
Create Date: 2026-10-31 15:00:00

"""

import re
import uuid

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026103102"
down_revision = "2026103101"
branch_labels = None
depends_on = None

OLD_LINK = re.compile(r"/attachments/(\d+)/")
NEW_LINK = re.compile(r"/attachments/([0-9a-f]{32})/")


def _relink(bind, pattern, mapping):
    """Rewrite every message's content & tool_trail through `mapping`."""
    messages = bind.execute(
        sa.text(
            "SELECT id, content, tool_trail FROM message "
            "WHERE content LIKE '%/attachments/%' OR tool_trail LIKE '%/attachments/%'"
        )
    ).fetchall()

    def swap(match):
        found = mapping.get(match.group(1))
        return f"/attachments/{found}/" if found else match.group(0)

    for message_id, content, trail in messages:
        new_content = pattern.sub(swap, content or "")
        new_trail = pattern.sub(swap, trail) if trail else trail
        if new_content != (content or "") or new_trail != trail:
            bind.execute(
                sa.text("UPDATE message SET content = :c, tool_trail = :t WHERE id = :i"),
                {"c": new_content, "t": new_trail, "i": message_id},
            )


def upgrade():
    with op.batch_alter_table("attachment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("public_id", sa.String(length=32), nullable=True))
    bind = op.get_bind()
    mapping = {}
    for (attachment_id,) in bind.execute(sa.text("SELECT id FROM attachment")):
        public_id = uuid.uuid4().hex
        mapping[str(attachment_id)] = public_id
        bind.execute(
            sa.text("UPDATE attachment SET public_id = :p WHERE id = :i"),
            {"p": public_id, "i": attachment_id},
        )
    _relink(bind, OLD_LINK, mapping)
    with op.batch_alter_table("attachment", schema=None) as batch_op:
        batch_op.alter_column("public_id", existing_type=sa.String(32), nullable=False)
        batch_op.create_index(
            batch_op.f("ix_attachment_public_id"), ["public_id"], unique=True
        )


def downgrade():
    bind = op.get_bind()
    mapping = {
        public_id: str(attachment_id)
        for attachment_id, public_id in bind.execute(
            sa.text("SELECT id, public_id FROM attachment")
        )
    }
    _relink(bind, NEW_LINK, mapping)
    with op.batch_alter_table("attachment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_attachment_public_id"))
        batch_op.drop_column("public_id")
