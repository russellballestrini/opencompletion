"""a deleted row's id is never handed out again: AUTOINCREMENT on room,
message, attachment & sandbox, each sequence seeded past every id our
chain ever saw

SQLite without AUTOINCREMENT gives a new row max(id)+1, so deleting the
newest room (or message) freed its id. On 2026-11-12 an exported chat's
room 634 & messages 4096-4101 were deleted, & the next chat was handed
the same ids: the export's ids & its /api/rooms/634/chain.jsonl link now
led to someone else's conversation.

Each table is rebuilt in batch mode (create the new table, copy every
row, drop the old, rename) with every column, index, unique & foreign key
kept, checked before & after: any difference raises & stops the upgrade,
& db_migrate's copy from before is kept. sqlite_sequence is then seeded
above both the table's largest id & every id the room chain recorded for
it, deleted rooms' chains included (room_event lines: roomId, messageId,
attachmentId & the ids a fork or edit & resend names).

Why these four: their rows go when a room is deleted, & their ids leave
our database (room & message ids in exports, URLs & chain lines;
attachment ids in chain lines; a sandbox id in a reply's resume
checkpoint & in timers armed for it). room_event & room_checkpoint are
insert-only, never deleted, so max+1 never repeats; activity_state &
user_session rows are deleted with a room but their ids are never shown,
stored or sent anywhere (both are found by room_id).

Postgres: nothing to do, its sequences never hand out an id twice.

Revision ID: 2026111201
Revises: 2026111001
Create Date: 2026-11-12 14:30:00

"""

import json

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "2026111201"
down_revision = "2026111001"
branch_labels = None
depends_on = None

TABLES = ("room", "message", "attachment", "sandbox")

# Chain line keys naming each table's ids (chain.py's callers: app.py,
# routes/rooms.py, blob_store.py). A key ending in MessageId, RoomId or
# AttachmentId (a later kind of event) counts too.
CHAIN_KEYS = {
    "room": {"roomId", "fromRoomId"},
    "message": {
        "messageId",
        "fromMessageId",
        "uptoMessageId",
        "revisionId",
        "supersededIds",
    },
    "attachment": {"attachmentId"},
}
SUFFIXES = {"room": "RoomId", "message": "MessageId", "attachment": "AttachmentId"}

# Other columns holding a table's ids, maybe a deleted row's.
ID_COLUMNS = {
    "room": [
        ("room_event", "room_id"),
        ("room_checkpoint", "room_id"),
        ("message", "room_id"),
        ("attachment", "room_id"),
        ("sandbox", "room_id"),
        ("activity_state", "room_id"),
        ("user_session", "room_id"),
        ("room", "forked_from_id"),
    ],
    "message": [
        ("room_event", "message_id"),
        ("message", "superseded_by_id"),
        ("message", "revision_of_id"),
    ],
    "attachment": [],
    "sandbox": [],
}


def _sqlite():
    return op.get_bind().dialect.name == "sqlite"


def _has_autoincrement(conn, table):
    sql = conn.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    ).scalar()
    return bool(sql) and "AUTOINCREMENT" in sql.upper()


def _shape(conn, table):
    """Everything a rebuild must keep, as comparable values."""
    inspector = sa.inspect(conn)
    norm = lambda rows: sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)
    return {
        "columns": norm(
            {
                "name": c["name"],
                "type": str(c["type"]),
                "nullable": c["nullable"],
                "default": c.get("default"),
            }
            for c in inspector.get_columns(table)
        ),
        "pk": inspector.get_pk_constraint(table)["constrained_columns"],
        "fks": norm(
            {
                "cols": f["constrained_columns"],
                "ref": f["referred_table"],
                "refcols": f["referred_columns"],
                "options": f.get("options"),
            }
            for f in inspector.get_foreign_keys(table)
        ),
        "uniques": norm(u["column_names"] for u in inspector.get_unique_constraints(table)),
        "indexes": norm(
            {"name": i["name"], "cols": i["column_names"], "unique": bool(i["unique"])}
            for i in inspector.get_indexes(table)
        ),
        "checks": norm(c["sqltext"] for c in inspector.get_check_constraints(table)),
        # Dropping a table drops its triggers; batch mode never remakes them.
        "triggers": sorted(
            tuple(row)
            for row in conn.execute(
                sa.text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'trigger' AND tbl_name = :t"
                ),
                {"t": table},
            )
        ),
        "rows": conn.execute(
            sa.text(f"SELECT count(*), coalesce(sum(id), 0) FROM {table}")
        ).one()._tuple(),
    }


def _ids_in(value, table, found):
    """Every id under `table`'s chain keys anywhere in a parsed line."""
    keys, suffix = CHAIN_KEYS[table], SUFFIXES[table]
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in keys or key.endswith(suffix) or key.endswith(suffix + "s"):
                for one in inner if isinstance(inner, list) else [inner]:
                    if isinstance(one, int) and not isinstance(one, bool):
                        found.append(one)
            elif isinstance(inner, (dict, list)):
                _ids_in(inner, table, found)
    elif isinstance(value, list):
        for inner in value:
            _ids_in(inner, table, found)


def _tables(conn):
    return set(sa.inspect(conn).get_table_names())


def highest_ids(conn):
    """{table: the largest id it or our chain ever held}, deleted rows'
    included as far as anything recorded them."""
    present = _tables(conn)
    high = {t: 0 for t in TABLES}
    for table in TABLES:
        high[table] = conn.execute(
            sa.text(f"SELECT coalesce(max(id), 0) FROM {table}")
        ).scalar()
        for other, column in ID_COLUMNS[table]:
            if other in present:
                value = conn.execute(
                    sa.text(f"SELECT max({column}) FROM {other}")
                ).scalar()
                high[table] = max(high[table], value or 0)
    if "room_event" not in present:
        return high
    # A chain is `<room id>@<created_at>`; its lines name the rest. One
    # pass over the lines, streamed: about a line per message & tool call.
    result = conn.execution_options(stream_results=True).execute(
        sa.text("SELECT chain, line FROM room_event")
    )
    for chain_key, line in result:
        head = (chain_key or "").split("@", 1)[0]
        if head.isdigit():
            high["room"] = max(high["room"], int(head))
        try:
            body = json.loads(line)
        except (TypeError, ValueError):
            continue
        for table in CHAIN_KEYS:
            found = []
            _ids_in(body, table, found)
            if found:
                high[table] = max(high[table], max(found))
    return high


def seed(conn, table, floor):
    """Raise `table`'s sequence to at least `floor`, never lower it."""
    current = conn.execute(
        sa.text("SELECT seq FROM sqlite_sequence WHERE name = :t"), {"t": table}
    ).fetchall()
    if not current:
        conn.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :s)"),
            {"t": table, "s": floor},
        )
    elif current[0][0] < floor:
        conn.execute(
            sa.text("UPDATE sqlite_sequence SET seq = :s WHERE name = :t"),
            {"t": table, "s": floor},
        )


def _rebuild(conn, table, autoincrement):
    before = _shape(conn, table)
    # Batch mode carries a NAMED check over itself; an unnamed one (an
    # older SQLAlchemy's Boolean) only when handed to it.
    unnamed = [
        sa.CheckConstraint(c["sqltext"])
        for c in sa.inspect(conn).get_check_constraints(table)
        if not c.get("name")
    ]
    with op.batch_alter_table(
        table,
        recreate="always",
        table_args=unnamed,
        table_kwargs={"sqlite_autoincrement": autoincrement},
    ):
        pass
    after = _shape(conn, table)
    if before != after:
        changed = sorted(k for k in before if before[k] != after.get(k))
        raise RuntimeError(
            f"rebuilding {table} would change its {', '.join(changed)}; "
            "stopped, nothing further migrated"
        )
    if _has_autoincrement(conn, table) != autoincrement:
        raise RuntimeError(f"rebuilding {table} did not set AUTOINCREMENT")


def _atomic(conn):
    """Open SQLite's transaction before the first rebuild. Python's sqlite3
    begins one only at the first write, so a rebuild's CREATE of its
    scratch table would commit on its own & outlive a failure. Also clears
    a scratch table such a failure left behind (batch mode's own name)."""
    conn.execute(sa.text("UPDATE alembic_version SET version_num = version_num"))
    for table in TABLES:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS _alembic_tmp_{table}"))


def upgrade():
    if not _sqlite():
        return
    conn = op.get_bind()
    _atomic(conn)
    present = _tables(conn)
    # Read before any rebuild: the ids a chain names stay put either way.
    high = highest_ids(conn)
    for table in TABLES:
        if table not in present:
            continue
        if not _has_autoincrement(conn, table):
            _rebuild(conn, table, True)
        seed(conn, table, high[table])


def downgrade():
    if not _sqlite():
        return
    conn = op.get_bind()
    _atomic(conn)
    present = _tables(conn)
    for table in TABLES:
        if table in present and _has_autoincrement(conn, table):
            _rebuild(conn, table, False)
