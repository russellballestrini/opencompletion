"""Our database brought up to date when our app starts, so a deploy needs
no hand-run `make upgrade-db` (fox, 2026-11-10: "we deploy automatically
and need a mechanism").

bring_up_to_date(app) looks at the database & does the one safe thing:

- empty (no tables): creates every table & stamps our newest migration;
- stamped at our newest migration: nothing;
- stamped behind it: copies a SQLite file aside first, then applies each
  migration in between;
- tables but no stamp (made by an old `make init-db`), or a stamp this
  code does not know (a database newer than this code, after a rollback):
  changes nothing & says what to do. We never guess a schema.

A migration that fails raises: our app does not start on a schema it
cannot use, & the copy made before it is still there. Returns one line
for our startup log (names & revisions only, never a database password).
OPENCOMPLETION_AUTO_MIGRATE=off skips all of it.
"""

import os
import sqlite3
import time


def auto_migrate_enabled():
    return (os.environ.get("OPENCOMPLETION_AUTO_MIGRATE") or "on").lower() != "off"


def _script(app):
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(app.extensions["migrate"].migrate.get_config())


def _stamp(db):
    """The revision a database is stamped at, or None with no stamp."""
    from sqlalchemy import inspect

    if "alembic_version" not in inspect(db.engine).get_table_names():
        return None
    row = db.session.execute(db.text("SELECT version_num FROM alembic_version")).first()
    db.session.rollback()
    return row[0] if row else None


def backup_sqlite(db, label):
    """Copy a SQLite file database beside itself before it changes; the
    copy's path, or None for a database that is not a file (in memory,
    another engine: its own backups are its keeper's)."""
    url = db.engine.url
    path = url.database
    if not url.drivername.startswith("sqlite") or not path or path == ":memory:":
        return None
    if not os.path.isfile(path):
        return None
    copy = f"{path}.before-{label}.bak"
    if os.path.exists(copy):
        copy = f"{path}.before-{label}.{int(time.time())}.bak"
    # sqlite's own backup: a consistent copy even in WAL mode, mid-write.
    source = sqlite3.connect(path)
    try:
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.chmod(copy, 0o600)
    return copy


def bring_up_to_date(app):
    """See this module's docstring. Call inside nothing: it opens its own
    app context."""
    if not auto_migrate_enabled():
        return "Database migrations: left alone (OPENCOMPLETION_AUTO_MIGRATE=off)"
    from flask_migrate import stamp, upgrade
    from sqlalchemy import inspect

    db = app.extensions["sqlalchemy"]
    with app.app_context():
        script = _script(app)
        head = script.get_current_head()
        tables = set(inspect(db.engine).get_table_names()) - {"alembic_version"}
        current = _stamp(db)

        if not tables:
            db.create_all()
            stamp(revision="head")
            return f"Database migrations: new database created at {head}"

        if current is None:
            # An old `make init-db` made these tables: which migration
            # they match is a person's call (README, "Upgrading").
            db.create_all()
            return (
                "[WARN] Database migrations: tables but no migration stamp, so "
                "nothing was migrated. Stamp the revision its schema matches "
                "(README: Upgrading an existing database), then restart"
            )

        if current == head:
            return f"Database migrations: up to date at {head}"

        try:
            script.get_revision(current)
        except Exception:  # alembic's own error types vary by version
            return (
                f"[WARN] Database migrations: stamped {current}, which this "
                f"code does not know (our newest is {head}); nothing was "
                "changed. A database newer than its code?"
            )

        copy = backup_sqlite(db, current)
        db.session.remove()
        db.engine.dispose()
        upgrade()
        now = _stamp(db)
        if now != head:
            raise RuntimeError(
                f"database migration stopped at {now}, short of {head}"
                + (f"; a copy from before is at {copy}" if copy else "")
            )
        return f"Database migrations: {current} -> {head}" + (
            f" (a copy from before: {copy})" if copy else ""
        )
