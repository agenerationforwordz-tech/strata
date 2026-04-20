# STRATA — Database Layer Router
# Selects SQLite or PostgreSQL backend based on STRATA_DB_BACKEND env var.
# Default: sqlite (works out of the box, no external DB needed)
# Production: set STRATA_DB_BACKEND=postgresql for concurrent multi-agent access
#
# Both backends expose identical function signatures — server.py works unchanged.

import os

DB_BACKEND = os.environ.get("STRATA_DB_BACKEND", "sqlite").lower()

if DB_BACKEND == "postgresql":
    from db_pg import *  # noqa: F401,F403
    print(f"[db] Backend: PostgreSQL (concurrent, scalable)")
else:
    from db_sqlite import *  # noqa: F401,F403
    print(f"[db] Backend: SQLite (simple, single-writer)")
