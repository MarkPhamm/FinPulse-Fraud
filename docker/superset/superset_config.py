"""
Superset config for the FinPulse local stack.

Mounted into the container at /app/pythonpath/superset_config.py. The Superset
entrypoint adds /app/pythonpath to PYTHONPATH and imports this module, which
overrides defaults from superset.config.

Class-project scope: SQLite metadata DB on a named volume (single-user). For
multi-user dashboard editing, switch SQLALCHEMY_DATABASE_URI to a Postgres URL
and create a `superset` database in the existing postgres container.
"""
import os

# Metadata DB. SQLite file lives on the `superset-data` named volume so it
# survives container restarts but is wiped by `make nuke`.
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"

# Required by Flask. Pulled from env (set in docker-compose.yml) so the value
# isn't hard-coded into the repo. Dev only — rotate for any non-local use.
SECRET_KEY = os.environ.get("SUPERSET_SECRET_KEY", "dev-secret-change-me")

# Superset's web server sits behind no proxy in this setup; disable the
# X-Forwarded-* trust to silence a warning at boot.
ENABLE_PROXY_FIX = False

# Don't surface the example dashboards / dummy data in the UI.
FEATURE_FLAGS = {}
