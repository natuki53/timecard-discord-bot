import datetime
import glob
import os

import aiosqlite


MEMBER_DIRECTORY_SCHEMA = """
    CREATE TABLE IF NOT EXISTS member_directory (
        guild_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        display_name TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )
"""


def normalize_display_name(value):
    normalized = " ".join(str(value or "").split()).strip()
    return normalized[:100] or "Unknown member"


async def init_member_directory(active_db_path):
    async with aiosqlite.connect(active_db_path) as conn:
        await conn.execute(MEMBER_DIRECTORY_SCHEMA)
        await conn.commit()


async def upsert_member(active_db_path, guild_id, user_id, display_name, *, now=None):
    timestamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    async with aiosqlite.connect(active_db_path) as conn:
        await conn.execute(MEMBER_DIRECTORY_SCHEMA)
        await conn.execute(
            """
            INSERT INTO member_directory (guild_id, user_id, display_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (int(guild_id), int(user_id), normalize_display_name(display_name), timestamp),
        )
        await conn.commit()


async def list_unresolved_member_ids(db_dir, active_db_path):
    known = set()
    discovered = set()

    async with aiosqlite.connect(active_db_path) as conn:
        await conn.execute(MEMBER_DIRECTORY_SCHEMA)
        async with conn.execute(
            "SELECT guild_id, user_id FROM member_directory"
        ) as cursor:
            known.update((int(row[0]), int(row[1])) for row in await cursor.fetchall())
        async with conn.execute(
            "SELECT guild_id, user_id FROM active_sessions WHERE guild_id > 0"
        ) as cursor:
            discovered.update((int(row[0]), int(row[1])) for row in await cursor.fetchall())
        await conn.commit()

    for db_path in sorted(glob.glob(os.path.join(db_dir, "work_tracking_????_??.db"))):
        month_key = os.path.basename(db_path)[len("work_tracking_") : -len(".db")]
        table_name = f"history_{month_key}"
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            async with conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ) as cursor:
                if not await cursor.fetchone():
                    continue
            async with conn.execute(
                f"""
                SELECT DISTINCT guild_id, user_id
                FROM {table_name}
                WHERE guild_id IS NOT NULL AND guild_id > 0
                """
            ) as cursor:
                discovered.update(
                    (int(row[0]), int(row[1])) for row in await cursor.fetchall()
                )

    return sorted(discovered - known)
