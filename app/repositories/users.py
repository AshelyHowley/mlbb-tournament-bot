from __future__ import annotations

from typing import Optional

# # import asyncpg  # removed for SQLite  # removed for SQLite

from app.utils.text import normalize_username


async def upsert_user(conn: asyncpg.Connection, tg_id: int, username: Optional[str], full_name: str) -> None:
    norm = normalize_username(username) if username else None
    await conn.execute(
        """
        INSERT INTO users (telegram_id, username, username_norm, full_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_id) DO UPDATE
           SET username = EXCLUDED.username, username_norm = EXCLUDED.username_norm,
               full_name = EXCLUDED.full_name, last_seen_at = datetime('now'), is_blocked = FALSE
        """,
        tg_id, username if norm else None, norm, (full_name or "")[:200],
    )


async def mark_blocked(conn: asyncpg.Connection, tg_id: int) -> None:
    await conn.execute("UPDATE users SET is_blocked = TRUE WHERE telegram_id = $1", tg_id)
