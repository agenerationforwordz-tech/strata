# STRATA — Agent Keys Router
# Selects SQLite or PostgreSQL backend based on STRATA_DB_BACKEND env var.

import os

DB_BACKEND = os.environ.get("STRATA_DB_BACKEND", "sqlite").lower()

if DB_BACKEND == "postgresql":
    from agent_keys_pg import *  # noqa: F401,F403
else:
    from agent_keys_sqlite import *  # noqa: F401,F403
