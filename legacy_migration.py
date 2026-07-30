import datetime
import os
from dataclasses import dataclass

import aiosqlite


LEGACY_USERS_MIGRATION_KEY = "legacy_users_to_active_sessions_v1"
LEGACY_GUILD_SETTING_KEY = "legacy_guild_id"


@dataclass(frozen=True)
class LegacySession:
    user_id: int
    start_time: str
    is_on_break: int
    break_start_time: str | None
    total_break_duration: float


async def _table_exists(conn, table_name):
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _latest_legacy_sessions(db_dir):
    sessions = {}
    if not os.path.isdir(db_dir):
        return sessions

    for filename in sorted(os.listdir(db_dir)):
        if not filename.startswith("work_tracking_") or not filename.endswith(".db"):
            continue
        db_path = os.path.join(db_dir, filename)
        async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            if not await _table_exists(conn, "users"):
                continue
            async with conn.execute(
                """
                SELECT id, start_time, is_on_break, break_start_time,
                       total_break_duration
                FROM users
                WHERE start_time IS NOT NULL
                """
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            user_id, start_time, is_on_break, break_start_time, break_duration = row
            candidate = LegacySession(
                user_id=int(user_id),
                start_time=start_time,
                is_on_break=int(is_on_break or 0),
                break_start_time=break_start_time,
                total_break_duration=float(break_duration or 0),
            )
            existing = sessions.get(candidate.user_id)
            if existing is None or candidate.start_time > existing.start_time:
                sessions[candidate.user_id] = candidate

    return sessions


def _migration_value(status):
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return f"{status}:{timestamp}"


async def migrate_legacy_users_once(db_dir, active_db_path, legacy_guild_id=0):
    """Import the legacy ``users`` tables at most once.

    Installations that already have ``legacy_guild_id`` were migrated by an
    earlier bot version. They only receive the completion marker; importing
    their stale monthly ``users`` rows again would resurrect finished shifts.
    """

    async with aiosqlite.connect(active_db_path) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            async with conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (LEGACY_USERS_MIGRATION_KEY,),
            ) as cursor:
                if await cursor.fetchone():
                    await conn.commit()
                    return {"status": "already_completed", "migrated": 0}

            async with conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (LEGACY_GUILD_SETTING_KEY,),
            ) as cursor:
                existing_installation = await cursor.fetchone()
            if existing_installation:
                await conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                    (
                        LEGACY_USERS_MIGRATION_KEY,
                        _migration_value("adopted_existing"),
                    ),
                )
                await conn.commit()
                return {"status": "adopted_existing", "migrated": 0}

            sessions = await _latest_legacy_sessions(db_dir)
            migrated = 0
            for session in sessions.values():
                async with conn.execute(
                    "SELECT 1 FROM active_sessions WHERE user_id = ?",
                    (session.user_id,),
                ) as cursor:
                    if await cursor.fetchone():
                        continue

                await conn.execute(
                    """
                    INSERT INTO active_sessions (
                        guild_id, user_id, start_time, is_on_break,
                        break_start_time, total_break_duration
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(legacy_guild_id),
                        session.user_id,
                        session.start_time,
                        session.is_on_break,
                        session.break_start_time,
                        session.total_break_duration,
                    ),
                )
                if session.is_on_break and session.break_start_time:
                    async with conn.execute(
                        """
                        SELECT 1
                        FROM break_records
                        WHERE guild_id = ? AND user_id = ? AND break_start = ?
                          AND break_end IS NULL
                        """,
                        (
                            int(legacy_guild_id),
                            session.user_id,
                            session.break_start_time,
                        ),
                    ) as cursor:
                        break_exists = await cursor.fetchone()
                    if not break_exists:
                        await conn.execute(
                            """
                            INSERT INTO break_records (
                                guild_id, user_id, break_start
                            )
                            VALUES (?, ?, ?)
                            """,
                            (
                                int(legacy_guild_id),
                                session.user_id,
                                session.break_start_time,
                            ),
                        )
                migrated += 1

            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                (
                    LEGACY_USERS_MIGRATION_KEY,
                    _migration_value("completed"),
                ),
            )
            await conn.commit()
            return {"status": "completed", "migrated": migrated}
        except Exception:
            await conn.rollback()
            raise
