from __future__ import annotations


async def get(conn, key: str, default: str = "") -> str:
    v = await conn.fetchval("SELECT value FROM settings WHERE key = $1", key)
    return default if v is None else v


async def set_(conn, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES ($1,$2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        key, value,
    )
