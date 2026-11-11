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
