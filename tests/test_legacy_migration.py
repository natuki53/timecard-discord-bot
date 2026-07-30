import asyncio
import os
import sqlite3
import tempfile
import unittest

from legacy_migration import (
    LEGACY_USERS_MIGRATION_KEY,
    migrate_legacy_users_once,
)


def create_active_db(path, *, legacy_guild_id=None):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                start_time TEXT,
                is_on_break INTEGER,
                break_start_time TEXT,
                total_break_duration REAL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE break_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                break_start TEXT NOT NULL,
                break_end TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        if legacy_guild_id is not None:
            conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                ("legacy_guild_id", str(legacy_guild_id)),
            )


def create_month_db(path, sessions):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                start_time TEXT,
                is_on_break INTEGER,
                break_start_time TEXT,
                total_break_duration REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO users (
                id, start_time, is_on_break, break_start_time,
                total_break_duration
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            sessions,
        )


class LegacyMigrationTests(unittest.TestCase):
    def test_imports_latest_legacy_session_only_once(self):
        with tempfile.TemporaryDirectory() as db_dir:
            active_path = os.path.join(db_dir, "active_sessions.db")
            create_active_db(active_path)
            create_month_db(
                os.path.join(db_dir, "work_tracking_2025_12.db"),
                [(10, "2025-12-20 10:00:00", 0, None, 0)],
            )
            create_month_db(
                os.path.join(db_dir, "work_tracking_2026_01.db"),
                [
                    (10, "2026-01-02 09:00:00", 1, "2026-01-02 10:00:00", 0),
                    (20, "2026-01-03 09:00:00", 0, None, 30),
                ],
            )

            result = asyncio.run(
                migrate_legacy_users_once(db_dir, active_path, legacy_guild_id=0)
            )
            self.assertEqual(result, {"status": "completed", "migrated": 2})

            with sqlite3.connect(active_path) as conn:
                active = conn.execute(
                    """
                    SELECT user_id, start_time, is_on_break
                    FROM active_sessions
                    ORDER BY user_id
                    """
                ).fetchall()
                breaks = conn.execute(
                    "SELECT user_id, break_start FROM break_records"
                ).fetchall()
                marker = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (LEGACY_USERS_MIGRATION_KEY,),
                ).fetchone()
                conn.execute("DELETE FROM active_sessions")
                conn.commit()

            self.assertEqual(
                active,
                [
                    (10, "2026-01-02 09:00:00", 1),
                    (20, "2026-01-03 09:00:00", 0),
                ],
            )
            self.assertEqual(breaks, [(10, "2026-01-02 10:00:00")])
            self.assertTrue(marker[0].startswith("completed:"))

            repeated = asyncio.run(
                migrate_legacy_users_once(db_dir, active_path, legacy_guild_id=0)
            )
            self.assertEqual(
                repeated, {"status": "already_completed", "migrated": 0}
            )
            with sqlite3.connect(active_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0],
                    0,
                )

    def test_marks_existing_installation_without_reimporting(self):
        with tempfile.TemporaryDirectory() as db_dir:
            active_path = os.path.join(db_dir, "active_sessions.db")
            create_active_db(active_path, legacy_guild_id=1234)
            create_month_db(
                os.path.join(db_dir, "work_tracking_2026_01.db"),
                [(10, "2026-01-02 09:00:00", 0, None, 0)],
            )

            result = asyncio.run(
                migrate_legacy_users_once(db_dir, active_path, legacy_guild_id=0)
            )
            self.assertEqual(
                result, {"status": "adopted_existing", "migrated": 0}
            )
            with sqlite3.connect(active_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0],
                    0,
                )
                marker = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (LEGACY_USERS_MIGRATION_KEY,),
                ).fetchone()
            self.assertTrue(marker[0].startswith("adopted_existing:"))

    def test_existing_active_session_is_preserved_and_not_reimported_later(self):
        with tempfile.TemporaryDirectory() as db_dir:
            active_path = os.path.join(db_dir, "active_sessions.db")
            create_active_db(active_path)
            with sqlite3.connect(active_path) as conn:
                conn.execute(
                    """
                    INSERT INTO active_sessions (
                        guild_id, user_id, start_time, is_on_break,
                        break_start_time, total_break_duration
                    )
                    VALUES (99, 10, '2026-02-01 09:00:00', 0, NULL, 0)
                    """
                )
            create_month_db(
                os.path.join(db_dir, "work_tracking_2026_01.db"),
                [(10, "2026-01-02 09:00:00", 0, None, 0)],
            )

            result = asyncio.run(
                migrate_legacy_users_once(db_dir, active_path, legacy_guild_id=0)
            )
            self.assertEqual(result, {"status": "completed", "migrated": 0})

            with sqlite3.connect(active_path) as conn:
                current = conn.execute(
                    "SELECT guild_id, start_time FROM active_sessions WHERE user_id = 10"
                ).fetchone()
                conn.execute("DELETE FROM active_sessions WHERE user_id = 10")
                conn.commit()
            self.assertEqual(current, (99, "2026-02-01 09:00:00"))

            repeated = asyncio.run(
                migrate_legacy_users_once(db_dir, active_path, legacy_guild_id=0)
            )
            self.assertEqual(
                repeated, {"status": "already_completed", "migrated": 0}
            )
            with sqlite3.connect(active_path) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM active_sessions").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
