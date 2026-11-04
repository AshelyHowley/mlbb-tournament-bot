"""Forward-only migration runner for SQLite."""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from app.database.pool import create_pool

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


async def migrate(pool) -> list[str]:
    applied_now: list[str] = []
    conn = await pool.acquire()
    try:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        done_rows = await conn.fetch("SELECT version FROM schema_migrations")
        done = {r["version"] for r in done_rows}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done or path.name.startswith("postgres"):
                continue
            sql = path.read_text(encoding="utf-8")
            # Strip full-line comments, then split statements
            lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
            clean = "\n".join(lines)
            for stmt in [s.strip() for s in clean.split(";") if s.strip()]:
                await conn.execute(stmt)
            await conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)", path.stem
            )
            applied_now.append(path.stem)
            log.info("Applied migration %s", path.stem)
    finally:
        await conn.close()
    return applied_now


async def _cli() -> int:
    from app.config import ConfigError, load_database_url
    logging.basicConfig(level=logging.INFO)
    try:
        dsn = load_database_url()
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2
    pool = await create_pool(dsn)
    try:
        applied = await migrate(pool)
        print("Applied:", applied or "nothing to do")
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_cli()))
