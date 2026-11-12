"""Our database follows our code at startup (db_migrate.py): a new one is
created & stamped, one behind is copied aside & migrated, & one we can't
place is left alone. Each case runs in its own interpreter against its own
SQLite file, as a deploy's restart would."""

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def run(database, code, **env):
    """Run `code` with our app imported against `database`; its stdout."""
    script = "import app, db_migrate\nfrom models import db\n" + textwrap.dedent(code)
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database}",
            "OPENCOMPLETION_SKIP_MODEL_MAP": "1",
            **env,
        },
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


def up(database, **env):
    out = run(
        database,
        "print(db_migrate.bring_up_to_date(app.app)); print('LAST', db_migrate.LAST)",
        **env,
    )
    up.last = [line for line in out.splitlines() if line.startswith("LAST ")][-1]
    return [line for line in out.splitlines() if "Database migrations" in line][-1]


def stamp(database):
    with sqlite3.connect(database) as con:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]


def columns(database, table):
    with sqlite3.connect(database) as con:
        return {row[1] for row in con.execute(f"pragma table_info({table})")}


@pytest.fixture(scope="module")
def head(tmp_path_factory):
    database = tmp_path_factory.mktemp("head") / "chat.db"
    assert "new database created at" in up(database)
    return stamp(database)


def test_a_new_database_is_created_and_stamped_then_left_alone(tmp_path, head):
    database = tmp_path / "chat.db"
    assert up(database) == f"Database migrations: new database created at {head}"
    assert stamp(database) == head and "content" in columns(database, "message")
    # A second start (a restart, our container's init then app) changes nothing.
    assert up(database) == f"Database migrations: up to date at {head}"
    assert not list(tmp_path.glob("*.bak"))


def test_a_database_behind_is_copied_aside_then_migrated(tmp_path, head):
    database = tmp_path / "chat.db"
    up(database)
    # Back one migration, with a message a migration must not lose.
    run(
        database,
        """
        from flask_migrate import downgrade
        from models import Message, Room
        with app.app.app_context():
            room = Room(name="lobby")
            db.session.add(room)
            db.session.commit()
            db.session.add(Message(username="ada", content="kept", room_id=room.id))
            db.session.commit()
            db.session.remove()
            downgrade(revision="-1")
        """,
    )
    behind = stamp(database)
    assert behind != head

    line = up(database)

    assert line.startswith(f"Database migrations: {behind} -> {head}")
    assert up.last == f"LAST {{'state': 'migrated', 'revision': '{head}'}}"
    assert stamp(database) == head
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT content FROM message").fetchall() == [("kept",)]
    # What it was before is beside it, readable only by us.
    (copy,) = tmp_path.glob("*.bak")
    assert copy.name == f"chat.db.before-{behind}.bak" and str(copy) in line
    assert stamp(copy) == behind
    assert oct(copy.stat().st_mode & 0o777) == "0o600"


def test_tables_with_no_stamp_are_never_guessed_at(tmp_path, head):
    database = tmp_path / "chat.db"
    up(database)
    with sqlite3.connect(database) as con:
        con.execute("DROP TABLE alembic_version")
    line = up(database)
    assert line.startswith("[WARN]") and "no migration stamp" in line
    with sqlite3.connect(database) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "alembic_version" not in tables and not list(tmp_path.glob("*.bak"))


def test_a_stamp_our_code_does_not_know_is_left_alone(tmp_path, head):
    database = tmp_path / "chat.db"
    up(database)
    with sqlite3.connect(database) as con:
        con.execute("UPDATE alembic_version SET version_num = '2099010101'")
    line = up(database)
    assert line.startswith("[WARN]") and "2099010101" in line
    assert stamp(database) == "2099010101" and not list(tmp_path.glob("*.bak"))


def test_it_can_be_switched_off(tmp_path):
    database = tmp_path / "chat.db"
    line = up(database, OPENCOMPLETION_AUTO_MIGRATE="off")
    assert "left alone" in line and not database.exists()


# Ids are never handed out twice (migration 2026111201): on 2026-11-12 a
# deleted chat's room 634 & messages 4096-4101 went to the next chat, so an
# export's ids & chain link led to someone else's conversation.
NEVER_REUSED = ("room", "message", "attachment", "sandbox")


def autoincrement(database):
    with sqlite3.connect(database) as con:
        return {
            name: "AUTOINCREMENT" in sql.upper()
            for name, sql in con.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            )
            if name in NEVER_REUSED
        }


def schema(database):
    """Every index & foreign key, & each table's columns: what a rebuild
    must keep."""
    with sqlite3.connect(database) as con:
        indexes = sorted(
            con.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'index'"
            )
        )
        tables = [
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        # (table, from, to, on update, on delete, match): a key's own number
        # is its place in the CREATE, which a rebuild may reorder.
        keys = {
            t: sorted(r[2:] for r in con.execute(f"pragma foreign_key_list({t})"))
            for t in tables
        }
        cols = {t: list(con.execute(f"pragma table_info({t})")) for t in tables}
    return indexes, keys, cols


def downgrade_one(database):
    run(
        database,
        """
        from flask_migrate import downgrade
        with app.app.app_context():
            downgrade(revision="-1")
        """,
    )


def test_a_fresh_database_never_hands_a_deleted_rows_id_out_again(tmp_path):
    database = tmp_path / "chat.db"
    up(database)
    assert autoincrement(database) == {t: True for t in NEVER_REUSED}
    out = run(
        database,
        """
        from models import Message, Room
        with app.app.app_context():
            room = Room(name="first")
            db.session.add(room)
            db.session.commit()
            msg = Message(username="ada", content="hi", room_id=room.id)
            db.session.add(msg)
            db.session.commit()
            ids = (room.id, msg.id)
            db.session.delete(msg)
            db.session.delete(room)
            db.session.commit()
            again = Room(name="second")
            db.session.add(again)
            db.session.commit()
            reply = Message(username="bo", content="yo", room_id=again.id)
            db.session.add(reply)
            db.session.commit()
            print("IDS", ids, (again.id, reply.id))
        """,
    )
    (line,) = [x for x in out.splitlines() if x.startswith("IDS ")]
    assert line == "IDS (1, 1) (2, 2)"


def old_head_with_a_deleted_chat(database):
    """A database at the head before AUTOINCREMENT, holding two rooms & a
    deleted one (id 634) whose chain names its ids, as the 3090's did."""
    up(database)
    downgrade_one(database)
    assert stamp(database) == "2026111001"
    assert autoincrement(database) == {t: False for t in NEVER_REUSED}
    lines = [
        ("user", {"roomId": 634, "messageId": 4096}),
        ("attachment", {"roomId": 634, "messageId": 4097, "attachmentId": 88}),
        (
            "supersede",
            {"roomId": 634, "revisionId": 4100, "supersededIds": [4098, 4099]},
        ),
        ("assistant", {"roomId": 634, "messageId": 4101}),
        ("room_deleted", {"roomId": 634, "deletedBy": 9999}),
    ]
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO blob (sha256, size, data, created_at) "
            "VALUES ('b', 1, x'00', '2026-11-12')"
        )
        for room_id, name in ((1, "lobby"), (2, "kept")):
            con.execute(
                "INSERT INTO room (id, name, is_private, is_archived, created_at, "
                "updated_at) VALUES (?, ?, 0, 0, '2026-11-12', 1)",
                (room_id, name),
            )
        for msg_id in (10, 11):
            con.execute(
                "INSERT INTO message (id, username, content, room_id) "
                "VALUES (?, 'ada', 'kept words', 2)",
                (msg_id,),
            )
        con.execute(
            "INSERT INTO attachment (id, public_id, room_id, sha256, filename, "
            "mime_type, size, created_at) VALUES (5, 'p5', 2, 'b', 'a.txt', "
            "'text/plain', 1, '2026-11-12')"
        )
        for seq, (kind, body) in enumerate(lines):
            line = json.dumps({"type": kind, "chain": "634@2026-11-11", **body})
            con.execute(
                "INSERT INTO room_event (chain, room_id, room_name, seq, type, "
                "message_id, line, hash, created_at) VALUES "
                "('634@2026-11-11', 634, 'gone', ?, ?, ?, ?, ?, 1.0)",
                (seq, kind, body.get("messageId"), line, f"h{seq}"),
            )


def test_an_upgrade_seeds_ids_past_every_one_the_chain_ever_saw(tmp_path):
    database = tmp_path / "chat.db"
    old_head_with_a_deleted_chat(database)
    before = schema(database)
    with sqlite3.connect(database) as con:
        rows = {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in NEVER_REUSED + ("room_event",)
        }

    assert up(database).startswith("Database migrations: 2026111001 -> ")
    assert autoincrement(database) == {t: True for t in NEVER_REUSED}

    # Every row, index & foreign key kept; only the id columns' DDL moved.
    indexes, keys, cols = schema(database)
    assert indexes == before[0] and keys == before[1] and cols == before[2]
    with sqlite3.connect(database) as con:
        assert rows == {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in NEVER_REUSED + ("room_event",)
        }
        seqs = dict(con.execute("SELECT name, seq FROM sqlite_sequence"))
        assert con.execute("pragma integrity_check").fetchone() == ("ok",)
    assert seqs["room"] == 634 and seqs["message"] == 4101
    assert seqs["attachment"] == 88

    # The newest room deleted, a new one made: its ids are past them all.
    out = run(
        database,
        """
        import blob_store
        from models import Attachment, Message, Room
        with app.app.app_context():
            newest = Room(name="newest")
            db.session.add(newest)
            db.session.commit()
            gone = newest.id
            db.session.delete(newest)
            db.session.commit()
            room = Room(name="fresh")
            db.session.add(room)
            db.session.commit()
            msg = Message(username="ada", content="new", room_id=room.id)
            db.session.add(msg)
            att = Attachment(room_id=room.id, sha256="b", filename="f.txt",
                             mime_type="text/plain", size=1)
            db.session.add(att)
            db.session.commit()
            print("IDS", gone, room.id, msg.id, att.id)
        """,
    )
    (line,) = [x for x in out.splitlines() if x.startswith("IDS ")]
    assert line == "IDS 635 636 4102 89"


def test_a_rebuild_that_would_lose_something_changes_nothing(tmp_path):
    """A trigger dies with its table & batch mode never remakes it: the
    upgrade stops, & what it rebuilt before rolls back with it."""
    database = tmp_path / "chat.db"
    old_head_with_a_deleted_chat(database)
    with sqlite3.connect(database) as con:
        con.execute("CREATE TRIGGER noted AFTER INSERT ON message BEGIN SELECT 1; END")
    before = schema(database)
    script = "import app, db_migrate\nprint(db_migrate.bring_up_to_date(app.app))"
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **os.environ,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{database}",
            "OPENCOMPLETION_SKIP_MODEL_MAP": "1",
        },
    )
    assert done.returncode != 0
    assert "rebuilding message would change its triggers" in done.stderr
    assert stamp(database) == "2026111001"
    assert autoincrement(database) == {t: False for t in NEVER_REUSED}
    assert schema(database) == before
    with sqlite3.connect(database) as con:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert "noted" in names and not any(n.startswith("_alembic_tmp") for n in names)
