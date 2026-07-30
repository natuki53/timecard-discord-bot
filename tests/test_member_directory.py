import asyncio
import os
import sqlite3
import tempfile
import unittest

from member_directory import (
    init_member_directory,
    list_unresolved_member_ids,
    normalize_display_name,
    upsert_member,
)


class MemberDirectoryTests(unittest.TestCase):
    def test_normalize_display_name(self):
        self.assertEqual(normalize_display_name("  雨苺\n なつき  "), "雨苺 なつき")
        self.assertEqual(normalize_display_name(""), "Unknown member")
        self.assertEqual(len(normalize_display_name("x" * 150)), 100)

    def test_upsert_and_discovery(self):
        with tempfile.TemporaryDirectory() as db_dir:
            active_path = os.path.join(db_dir, "active_sessions.db")
            asyncio.run(init_member_directory(active_path))

            with sqlite3.connect(active_path) as conn:
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
                    "INSERT INTO active_sessions VALUES (10, 20, '', 0, NULL, 0)"
                )

            month_path = os.path.join(db_dir, "work_tracking_2026_07.db")
            with sqlite3.connect(month_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE history_2026_07 (
                        id INTEGER PRIMARY KEY,
                        guild_id INTEGER,
                        user_id INTEGER,
                        start_time TEXT,
                        end_time TEXT,
                        total_break_duration REAL,
                        work_duration REAL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO history_2026_07 VALUES (1, 10, 30, '', '', 0, 0)"
                )

            unresolved = asyncio.run(
                list_unresolved_member_ids(db_dir, active_path)
            )
            self.assertEqual(unresolved, [(10, 20), (10, 30)])

            asyncio.run(upsert_member(active_path, 10, 20, "First name"))
            asyncio.run(upsert_member(active_path, 10, 20, "Updated name"))
            unresolved = asyncio.run(
                list_unresolved_member_ids(db_dir, active_path)
            )
            self.assertEqual(unresolved, [(10, 30)])

            with sqlite3.connect(active_path) as conn:
                row = conn.execute(
                    """
                    SELECT display_name
                    FROM member_directory
                    WHERE guild_id = 10 AND user_id = 20
                    """
                ).fetchone()
            self.assertEqual(row, ("Updated name",))


if __name__ == "__main__":
    unittest.main()
