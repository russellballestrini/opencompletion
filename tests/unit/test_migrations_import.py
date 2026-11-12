"""Reading our migration scripts must never load app.py.

Our app reads every revision at startup (db_migrate.py). A script that
imports `app` loads a second copy of app.py as module `app` (the server runs
as __main__), and that copy's module setup rebinds activity.py's app to one
that never got --local-activities: Play on our Activities page started
nothing (2026-11-11, KeyError: 'LOCAL_ACTIVITIES')."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE = """
import sys
from alembic.script import ScriptDirectory
scripts = ScriptDirectory("migrations")
revisions = list(scripts.walk_revisions())
print(len(revisions), "app" in sys.modules)
"""


def test_reading_every_revision_never_imports_app():
    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    count, imported = done.stdout.split()
    assert int(count) > 10
    assert imported == "False"


def test_ids_never_reused_leaves_postgres_alone(monkeypatch):
    """Postgres sequences never hand out an id twice: migration 2026111201
    rebuilds & seeds SQLite only, & touches nothing else."""
    import importlib.util
    from types import SimpleNamespace

    path = os.path.join(
        ROOT, "migrations", "versions", "2026111201_ids_never_reused.py"
    )
    spec = importlib.util.spec_from_file_location("ids_never_reused", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    touched = []

    class Bind:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, *args, **kwargs):
            touched.append(args)

    fake = SimpleNamespace(
        get_bind=lambda: Bind(),
        batch_alter_table=lambda *a, **k: touched.append(a),
    )
    monkeypatch.setattr(module, "op", fake)
    module.upgrade()
    module.downgrade()
    assert touched == []
