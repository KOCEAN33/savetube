"""The savetube web app against the smoke run's database, login skipped.

uvicorn loads it with `--app-dir e2e`. SAVETUBE_E2E_DB names the database,
and the other SAVETUBE_* settings come from the environment.
"""

import os

from savetube.config import Settings
from savetube.web.app import create_app
from savetube.web.auth import User, current_user
from seed import test_dsn

app = create_app(Settings(database_url=test_dsn(os.environ["SAVETUBE_E2E_DB"])))
app.dependency_overrides[current_user] = lambda: User(sub="e2e", name="E2E", email=None)
