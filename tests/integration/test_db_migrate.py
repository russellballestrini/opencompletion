"""Our database follows our code at startup (db_migrate.py): a new one is
created & stamped, one behind is copied aside & migrated, & one we can't
place is left alone. Each case runs in its own interpreter against its own
SQLite file, as a deploy's restart would."""

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
