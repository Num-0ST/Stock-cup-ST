"""
Apply database upgrades automatically on startup.

Every file in backend/migrations/*.sql is idempotent, so it is safe on a fresh database
(schema.sql already contains everything) and on an older one being upgraded.
Applied files are recorded in schema_migrations; several workers starting together are
serialised with an advisory lock.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .db import get_pool

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
log = logging.getLogger("wms.migrate")


def run_migrations(directory: Path = MIGRATIONS_DIR) -> list[str]:
    files = sorted(directory.glob("*.sql")) if directory.is_dir() else []
    applied: list[str] = []
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(727002)")
            conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                                name VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
            for f in files:
                if f.name in done:
                    continue
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
                log.warning("applied migration %s", f.name)
    return applied
