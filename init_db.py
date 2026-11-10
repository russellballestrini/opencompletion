"""Create our database, or bring an existing one up to date: what
`make init-db` & our container run before serving (db_migrate.py)."""

from app import app

import db_migrate

print(db_migrate.bring_up_to_date(app))
