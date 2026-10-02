from contextlib import closing
import asyncio
import datetime
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiosqlite

import monthly_history


SPEC = importlib.util.spec_from_file_location(
    'timecard_monthly_test', Path(__file__).resolve().parents[1] / 'timecard-main.py'
)
timecard = importlib.util.module_from_spec(SPEC)
with tempfile.TemporaryDirectory() as import_db_dir:
    with patch.dict(os.environ, {'DB_DIR': import_db_dir, 'DISCORD_TOKEN': 'test-token'}):
        SPEC.loader.exec_module(timecard)


def dt(value):
    return datetime.datetime.strptime(value, monthly_history.TIMESTAMP_FORMAT)


class MonthlyHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_dir = self.temp.name
        self.active_path = os.path.join(self.db_dir, 'active_sessions.db')
        self.config = patch.multiple(timecard, DB_DIR=self.db_dir, ACTIVE_DB_PATH=self.active_path)
        self.config.start()
        await timecard.init_active_db()
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.execute("INSERT INTO app_settings VALUES ('legacy_guild_id', '10')")

    async def asyncTearDown(self):
        self.config.stop()
        self.temp.cleanup()

    def start_session(self, start, *, user=20, guild=10, on_break=0,
                      break_start=None, legacy_break=0):
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.execute('''
                INSERT INTO active_sessions (
                    guild_id, user_id, start_time, is_on_break,
                    break_start_time, total_break_duration
                ) VALUES (?, ?, ?, ?, ?, ?)
            ''', (guild, user, start, on_break, break_start, legacy_break))

    def add_break(self, start, end, *, user=20, guild=10):
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.execute('''
                INSERT INTO break_records (guild_id, user_id, break_start, break_end)
                VALUES (?, ?, ?, ?)
            ''', (guild, user, start, end))

    def active_rows(self):
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM active_sessions')]

    def history(self, month):
        path = os.path.join(self.db_dir, f'work_tracking_{month}.db')
        if not os.path.exists(path):
            return []
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f'SELECT * FROM history_{month}')]

    def pending_count(self):
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            return conn.execute('SELECT COUNT(*) FROM history_outbox').fetchone()[0]

    async def close(self, now):
        return await monthly_history.close_finished_months(
            self.db_dir, self.active_path, now=dt(now)
        )

    async def finish(self, now, *, user=20, guild=10):
        return await monthly_history.finish_session(
            self.db_dir, self.active_path, guild, user, now=dt(now)
        )

    async def test_midnight_preserves_session_and_has_no_missing_second(self):
        self.start_session('2026-09-30 23:59:59')
        self.assertEqual(await self.close('2026-10-01 00:00:00'), 1)
        self.assertEqual(await self.close('2026-10-01 00:00:30'), 0)
        september = self.history('2026_09')
        self.assertEqual(len(september), 1)
        self.assertEqual(september[0]['work_duration'], 1)
        self.assertEqual(september[0]['end_time'], '2026-10-01 00:00:00')
        session = self.active_rows()[0]
        self.assertEqual(session['start_time'], '2026-09-30 23:59:59')
        self.assertEqual(session['recorded_until'], '2026-10-01 00:00:00')
        result = await self.finish('2026-10-01 00:00:02')
        self.assertEqual(result, {'status': 'ended', 'work_duration': 3})
        self.assertEqual(self.history('2026_10')[0]['work_duration'], 2)
        self.assertEqual(self.active_rows(), [])

    async def test_open_break_crossing_midnight_is_preserved_and_split(self):
        self.start_session('2026-09-30 22:00:00', on_break=1,
                           break_start='2026-09-30 23:00:00')
        self.add_break('2026-09-30 23:00:00', None)
        await self.close('2026-10-01 00:00:30')
        self.assertEqual(self.history('2026_09')[0]['work_duration'], 3600)
        session = self.active_rows()[0]
        self.assertEqual(session['is_on_break'], 1)
        self.assertEqual(session['break_start_time'], '2026-09-30 23:00:00')
        self.assertEqual(await self.finish('2026-10-01 01:00:00'), {'status': 'on_break'})
        self.assertEqual(len(self.active_rows()), 1)
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.execute('UPDATE active_sessions SET is_on_break = 0')
            conn.execute("UPDATE break_records SET break_end = '2026-10-01 01:00:00'")
        result = await self.finish('2026-10-01 02:00:00')
        self.assertEqual(result['work_duration'], 7200)
        self.assertEqual(self.history('2026_10')[0]['work_duration'], 3600)

    async def test_restart_catches_up_intermediate_months(self):
        self.start_session('2026-09-30 23:00:00')
        await self.close('2026-12-02 08:00:00')
        self.assertEqual(len(self.history('2026_09')), 1)
        self.assertEqual(len(self.history('2026_10')), 1)
        self.assertEqual(len(self.history('2026_11')), 1)
        self.assertEqual(self.history('2026_12'), [])
        await self.close('2026-12-02 09:00:00')
        result = await self.finish('2026-12-02 10:00:00')
        recorded = sum(r['work_duration'] for month in ('2026_09', '2026_10', '2026_11', '2026_12')
                       for r in self.history(month))
        self.assertEqual(recorded, result['work_duration'])
        self.assertEqual(self.pending_count(), 0)

    async def test_crash_after_month_commit_does_not_duplicate_history(self):
        self.start_session('2026-09-30 23:00:00')
        with patch.object(monthly_history, '_acknowledge_history',
                          AsyncMock(side_effect=OSError('simulated crash'))):
            with self.assertRaises(OSError):
                await self.close('2026-10-01 00:00:00')
        self.assertEqual(self.pending_count(), 1)
        self.assertEqual(len(self.history('2026_09')), 1)
        self.assertEqual(self.active_rows()[0]['recorded_until'], '2026-10-01 00:00:00')
        await self.close('2026-10-01 01:00:00')
        self.assertEqual(len(self.history('2026_09')), 1)
        self.assertEqual(self.pending_count(), 0)

    async def test_failed_checkpoint_rolls_back_segment_and_cursor(self):
        self.start_session('2026-09-30 23:00:00')
        with closing(sqlite3.connect(self.active_path)) as conn, conn:
            conn.execute('''
                CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON active_sessions
                BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END
            ''')
        with self.assertRaises(sqlite3.IntegrityError):
            await self.close('2026-10-01 00:00:00')
        self.assertEqual(self.pending_count(), 0)
        self.assertIsNone(self.active_rows()[0]['recorded_until'])
        self.assertEqual(self.history('2026_09'), [])

    async def test_checkout_survives_unavailable_month_database(self):
        self.start_session('2026-09-30 23:00:00')
        with patch.object(monthly_history, 'flush_history',
                          AsyncMock(side_effect=OSError('month DB unavailable'))):
            with self.assertLogs('monthly_history', level='ERROR'):
                result = await self.finish('2026-10-01 01:00:00')
        self.assertEqual(result['work_duration'], 7200)
        self.assertEqual(self.active_rows(), [])
        self.assertEqual(self.pending_count(), 2)
        await self.close('2026-10-01 01:00:30')
        self.assertEqual(self.history('2026_09')[0]['work_duration'], 3600)
        self.assertEqual(self.history('2026_10')[0]['work_duration'], 3600)
        self.assertEqual(self.pending_count(), 0)

    async def test_simultaneous_close_and_checkout_do_not_double_count(self):
        self.start_session('2026-09-30 23:00:00')
        _, result = await asyncio.gather(
            self.close('2026-10-01 01:00:00'),
            self.finish('2026-10-01 01:00:00'),
        )
        self.assertEqual(result['work_duration'], 7200)
        self.assertEqual(sum(r['work_duration'] for r in self.history('2026_09')), 3600)
        self.assertEqual(sum(r['work_duration'] for r in self.history('2026_10')), 3600)
        self.assertEqual(self.pending_count(), 0)

    async def test_legacy_schema_and_break_duration_are_preserved(self):
        self.start_session('2026-09-30 23:00:00', legacy_break=1800)
        # 前バージョンの確定済み行は変更しない。
        with closing(sqlite3.connect(os.path.join(self.db_dir, 'work_tracking_2026_09.db'))) as conn, conn:
            conn.execute('''
                CREATE TABLE history_2026_09 (
                    id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER,
                    start_time TEXT, end_time TEXT,
                    total_break_duration REAL, work_duration REAL
                )
            ''')
            conn.execute("INSERT INTO history_2026_09 VALUES (1, 10, 20, 'old-start', 'old-end', 5, 100)")
        await timecard.init_active_db()
        await self.close('2026-10-01 00:00:00')
        await timecard.init_active_db()
        self.assertEqual(self.active_rows()[0]['legacy_break_remaining'], 0)
        self.add_break('2026-10-01 00:10:00', '2026-10-01 00:20:00')
        result = await self.finish('2026-10-01 01:00:00')
        self.assertEqual(result['work_duration'], 4800)
        self.assertEqual(self.history('2026_09')[0]['work_duration'], 100)
        self.assertEqual(sum(r['work_duration'] for r in self.history('2026_09')[1:])
                         + sum(r['work_duration'] for r in self.history('2026_10')), 4800)

    async def test_legacy_session_waits_for_guild_assignment(self):
        self.start_session('2026-09-30 23:00:00', guild=0)
        await self.close('2026-10-01 00:00:00')
        self.assertEqual(self.pending_count(), 0)
        self.assertEqual(self.history('2026_09'), [])

    async def test_commands_keep_break_state_and_checkout_total(self):
        self.start_session('2026-09-30 22:00:00', on_break=1,
                           break_start='2026-09-30 23:00:00')
        self.add_break('2026-09-30 23:00:00', None)
        response = SimpleNamespace(is_done=Mock(return_value=True))
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10), guild_id=10,
            user=SimpleNamespace(id=20, mention='<@20>', display_name='test'),
            response=response, edit_original_response=AsyncMock(), id=1,
        )
        with patch.object(monthly_history, 'now_jst', return_value=dt('2026-10-01 01:00:00')):
            with patch.object(timecard, 'now_jst', return_value=dt('2026-10-01 01:00:00')):
                await timecard.start.callback(interaction)
                self.assertIn('休憩中のため出勤できません', interaction.edit_original_response.call_args.kwargs['content'])
                await timecard.restart.callback(interaction)
        with patch.object(monthly_history, 'now_jst', return_value=dt('2026-10-01 02:00:00')):
            await timecard.end.callback(interaction)
        self.assertIn('勤務時間は 2時間0分', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(self.active_rows(), [])
        self.assertEqual(self.history('2026_09')[0]['work_duration'], 3600)
        self.assertEqual(self.history('2026_10')[0]['work_duration'], 3600)

    async def test_monthly_commands_include_closed_months_without_checkout(self):
        self.start_session('2026-09-30 23:00:00')
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10), guild_id=10,
            user=SimpleNamespace(id=20, mention='<@20>', display_name='test'),
            response=SimpleNamespace(is_done=Mock(return_value=True)),
            edit_original_response=AsyncMock(), id=1,
        )
        now = dt('2026-10-02 01:00:00')
        with patch.object(monthly_history, 'now_jst', return_value=now):
            with patch.object(timecard, 'now_jst', return_value=now):
                await timecard.last_monthly.callback(interaction)
                self.assertIn('先月の合計勤務時間は 1時間0分', interaction.edit_original_response.call_args.kwargs['content'])
                await timecard.monthly.callback(interaction)
                self.assertIn('今月の勤務履歴はありません', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(len(self.active_rows()), 1)

    async def test_background_close_and_existing_command_sequence(self):
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10), guild_id=10,
            user=SimpleNamespace(id=20, mention='<@20>', display_name='test'),
            response=SimpleNamespace(is_done=Mock(return_value=True)),
            edit_original_response=AsyncMock(), id=1,
        )

        async def command_at(command, timestamp):
            with patch.object(monthly_history, 'now_jst', return_value=dt(timestamp)):
                with patch.object(timecard, 'now_jst', return_value=dt(timestamp)):
                    await command.callback(interaction)

        await command_at(timecard.start, '2026-09-30 22:00:00')
        await command_at(timecard.break_, '2026-09-30 23:30:00')
        with patch.object(monthly_history, 'now_jst', return_value=dt('2026-10-01 00:00:00')):
            with patch.object(timecard, 'now_jst', return_value=dt('2026-09-30 23:59:59')):
                with patch.object(timecard.bot, 'is_closed', side_effect=[False, True]):
                    with patch.object(timecard.discord.utils, 'sleep_until', new=AsyncMock()) as wait:
                        await timecard.monthly_close()
                        wait.assert_awaited_once_with(dt('2026-10-01 00:00:00').replace(tzinfo=monthly_history.JST))
        self.assertEqual(self.history('2026_09')[0]['work_duration'], 5400)
        self.assertEqual(self.active_rows()[0]['is_on_break'], 1)
        await command_at(timecard.restart, '2026-10-01 01:00:00')
        await command_at(timecard.end, '2026-10-01 02:00:00')
        self.assertIn('勤務時間は 2時間30分', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertEqual(self.history('2026_10')[0]['work_duration'], 3600)
        self.assertEqual(self.active_rows(), [])

    async def test_ready_catches_up_without_starting_duplicate_background_tasks(self):
        self.start_session('2026-09-30 23:00:00')
        scheduled_task = Mock()
        scheduled_task.done.return_value = False

        def create_task(coro):
            coro.close()
            return scheduled_task

        with patch.object(monthly_history, 'now_jst', return_value=dt('2026-10-01 01:00:00')):
            with patch.object(timecard, 'monthly_close_task', None):
                with patch.object(timecard.asyncio, 'create_task', side_effect=create_task) as start_loop:
                    with patch.object(timecard.status_reporter, 'start'):
                        with patch.object(timecard.bot.tree, 'sync', new=AsyncMock()):
                            with patch.object(timecard, 'backfill_member_directory', new=AsyncMock()):
                                await timecard.on_ready()
                                await timecard.on_ready()
                    start_loop.assert_called_once()
        self.assertEqual(len(self.history('2026_09')), 1)
        self.assertEqual(len(self.active_rows()), 1)

    async def test_date_change_within_same_month_does_not_close_history(self):
        self.start_session('2026-09-23 14:51:01')
        for now in ('2026-09-24 00:00:00', '2026-09-30 23:59:59'):
            self.assertEqual(await self.close(now), 0)
            self.assertEqual(self.history('2026_09'), [])
            self.assertIsNone(self.active_rows()[0]['recorded_until'])
        self.assertEqual(await self.close('2026-10-01 00:00:00'), 1)
        self.assertEqual(len(self.history('2026_09')), 1)

    async def test_next_month_deadline_handles_year_end_and_leap_year(self):
        for before, after in (
            ('2026-12-31 23:59:59', '2027-01-01 00:00:00'),
            ('2028-02-29 23:59:59', '2028-03-01 00:00:00'),
            ('2026-02-28 12:00:00', '2026-03-01 00:00:00'),
        ):
            self.assertEqual(monthly_history.next_month_start(dt(before)), dt(after))


if __name__ == '__main__':
    unittest.main()
