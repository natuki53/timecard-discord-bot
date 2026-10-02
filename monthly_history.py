"""月末の勤務確定と、月別DBへの再実行可能な転記。"""

import datetime
import logging
import os
import re
import uuid

import aiosqlite


logger = logging.getLogger(__name__)
TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M:%S'
JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst():
    return datetime.datetime.now(JST).replace(tzinfo=None, microsecond=0)


def next_month_start(value):
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1, day=1,
                             hour=0, minute=0, second=0, microsecond=0)
    return value.replace(month=value.month + 1, day=1,
                         hour=0, minute=0, second=0, microsecond=0)


async def init_monthly_history_schema(conn):
    async with conn.execute('PRAGMA table_info(active_sessions)') as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if 'recorded_until' not in columns:
        await conn.execute('ALTER TABLE active_sessions ADD COLUMN recorded_until TEXT')
    if 'legacy_break_remaining' not in columns:
        await conn.execute(
            'ALTER TABLE active_sessions ADD COLUMN legacy_break_remaining REAL'
        )
    await conn.execute('''
        UPDATE active_sessions
        SET recorded_until = COALESCE(recorded_until, start_time),
            legacy_break_remaining = COALESCE(
                legacy_break_remaining, total_break_duration, 0
            )
    ''')
    await conn.execute('''
        CREATE TABLE IF NOT EXISTS history_outbox (
            segment_id TEXT PRIMARY KEY,
            month_key TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            total_break_duration REAL NOT NULL,
            work_duration REAL NOT NULL
        )
    ''')


async def _session_breaks(conn, session, until):
    async with conn.execute('''
        SELECT break_start, break_end FROM break_records
        WHERE guild_id = ? AND user_id = ? AND break_start >= ?
        ORDER BY break_start
    ''', (session['guild_id'], session['user_id'], session['start_time'])) as cursor:
        rows = await cursor.fetchall()
    intervals = [
        (datetime.datetime.strptime(start, TIMESTAMP_FORMAT),
         datetime.datetime.strptime(end, TIMESTAMP_FORMAT) if end else until)
        for start, end in rows
    ]
    # 旧DBに休憩状態だけ残っている場合も、休憩を勤務扱いしない。
    if session['is_on_break'] and session['break_start_time']:
        if not any(start == session['break_start_time'] and end is None
                   for start, end in rows):
            intervals.append((
                datetime.datetime.strptime(session['break_start_time'], TIMESTAMP_FORMAT),
                until,
            ))
    return intervals


def _break_seconds(start, end, intervals):
    """重複する休憩をまとめ、対象区間と重なる秒数だけ控除する。"""
    clipped = sorted(
        (max(start, break_start), min(end, break_end))
        for break_start, break_end in intervals
        if max(start, break_start) < min(end, break_end)
    )
    total = 0
    previous_end = start
    for break_start, break_end in clipped:
        total += max(0, (break_end - max(previous_end, break_start)).total_seconds())
        previous_end = max(previous_end, break_end)
    return total


async def _queue_until(conn, session, until):
    start = datetime.datetime.strptime(
        session['recorded_until'] or session['start_time'], TIMESTAMP_FORMAT
    )
    if until < start:
        raise ValueError('保存済みの勤務区間より前には締められません。')
    intervals = await _session_breaks(conn, session, until)
    legacy_remaining = session['legacy_break_remaining']
    if legacy_remaining is None:
        legacy_remaining = session['total_break_duration'] or 0
    count = 0
    while start < until:
        end = min(next_month_start(start), until)
        span = (end - start).total_seconds()
        recorded_break = _break_seconds(start, end, intervals)
        legacy_break = min(legacy_remaining, max(0, span - recorded_break))
        legacy_remaining -= legacy_break
        break_duration = recorded_break + legacy_break
        await conn.execute('''
            INSERT INTO history_outbox (
                segment_id, month_key, guild_id, user_id, start_time,
                end_time, total_break_duration, work_duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            uuid.uuid4().hex, start.strftime('%Y_%m'),
            session['guild_id'], session['user_id'],
            start.strftime(TIMESTAMP_FORMAT), end.strftime(TIMESTAMP_FORMAT),
            break_duration, max(0, span - break_duration),
        ))
        start = end
        count += 1
    await conn.execute('''
        UPDATE active_sessions
        SET recorded_until = ?, legacy_break_remaining = ?
        WHERE guild_id = ? AND user_id = ?
    ''', (until.strftime(TIMESTAMP_FORMAT), legacy_remaining,
          session['guild_id'], session['user_id']))
    return count


async def _ensure_month_schema(conn, month_key):
    if not re.fullmatch(r'\d{4}_(?:0[1-9]|1[0-2])', month_key):
        raise ValueError('Invalid history month')
    table = f'history_{month_key}'
    await conn.execute(f'''
        CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            total_break_duration REAL,
            work_duration REAL,
            segment_id TEXT
        )
    ''')
    async with conn.execute(f'PRAGMA table_info({table})') as cursor:
        columns = {row[1] for row in await cursor.fetchall()}
    if 'guild_id' not in columns:
        await conn.execute(f'ALTER TABLE {table} ADD COLUMN guild_id INTEGER')
    if 'segment_id' not in columns:
        await conn.execute(f'ALTER TABLE {table} ADD COLUMN segment_id TEXT')
    await conn.execute(
        f'CREATE UNIQUE INDEX IF NOT EXISTS {table}_segment_id ON {table}(segment_id)'
    )
    return table


async def _acknowledge_history(active_db_path, segment_ids):
    async with aiosqlite.connect(active_db_path) as conn:
        await conn.executemany(
            'DELETE FROM history_outbox WHERE segment_id = ?',
            [(segment_id,) for segment_id in segment_ids],
        )
        await conn.commit()


async def flush_history(db_dir, active_db_path):
    """転記後に停止しても、一意なsegment_idで重複を防いで再試行する。"""
    async with aiosqlite.connect(active_db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            'SELECT * FROM history_outbox ORDER BY month_key, start_time'
        ) as cursor:
            rows = await cursor.fetchall()
    by_month = {}
    for row in rows:
        by_month.setdefault(row['month_key'], []).append(row)
    for month_key, segments in by_month.items():
        if not re.fullmatch(r'\d{4}_(?:0[1-9]|1[0-2])', month_key):
            raise ValueError('Invalid history month')
        db_path = os.path.join(db_dir, f'work_tracking_{month_key}.db')
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            table = await _ensure_month_schema(conn, month_key)
            await conn.executemany(f'''
                INSERT INTO {table} (
                    guild_id, user_id, start_time, end_time,
                    total_break_duration, work_duration, segment_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(segment_id) DO NOTHING
            ''', [(
                r['guild_id'], r['user_id'], r['start_time'], r['end_time'],
                r['total_break_duration'], r['work_duration'], r['segment_id'],
            ) for r in segments])
            await conn.commit()
        await _acknowledge_history(active_db_path, [r['segment_id'] for r in segments])


async def close_finished_months(db_dir, active_db_path, *, now=None):
    now = now or now_jst()
    cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    queued = 0
    async with aiosqlite.connect(active_db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute('BEGIN IMMEDIATE')
        async with conn.execute('''
            SELECT * FROM active_sessions
            WHERE guild_id > 0 AND start_time < ?
              AND COALESCE(recorded_until, start_time) < ?
        ''', (cutoff.strftime(TIMESTAMP_FORMAT), cutoff.strftime(TIMESTAMP_FORMAT))) as cursor:
            sessions = await cursor.fetchall()
        for session in sessions:
            queued += await _queue_until(conn, session, cutoff)
        await conn.commit()
    await flush_history(db_dir, active_db_path)
    return queued


async def finish_session(db_dir, active_db_path, guild_id, user_id, *, now=None):
    until = now or now_jst()
    async with aiosqlite.connect(active_db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute('BEGIN IMMEDIATE')
        async with conn.execute('''
            SELECT * FROM active_sessions
            WHERE user_id = ? AND guild_id IN (?, 0)
            ORDER BY guild_id DESC
        ''', (user_id, guild_id)) as cursor:
            session = await cursor.fetchone()
        if session is None:
            return {'status': 'not_started'}
        if session['is_on_break']:
            return {'status': 'on_break'}
        start = datetime.datetime.strptime(session['start_time'], TIMESTAMP_FORMAT)
        breaks = await _session_breaks(conn, session, until)
        total_break = _break_seconds(start, until, breaks)
        total_break += session['total_break_duration'] or 0
        work_duration = max(0, (until - start).total_seconds() - total_break)
        await _queue_until(conn, session, until)
        await conn.execute(
            'DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?',
            (session['guild_id'], user_id),
        )
        await conn.execute('''
            DELETE FROM break_records
            WHERE guild_id = ? AND user_id = ? AND break_start >= ?
        ''', (session['guild_id'], user_id, session['start_time']))
        await conn.commit()
    try:
        await flush_history(db_dir, active_db_path)
    except Exception:
        # 勤務履歴は既にoutboxに永続化済み。次の締め・集計で転記を再試行する。
        logger.exception('Monthly history transfer pending after checkout')
    return {'status': 'ended', 'work_duration': work_duration}
