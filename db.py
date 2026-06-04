import aiosqlite
from typing import AsyncGenerator

async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    db = await aiosqlite.connect("cache.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")

    try:
        yield db
    finally:
        await db.close()

async def get_db_direct() -> aiosqlite.Connection:
    db = await aiosqlite.connect("cache.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    return db