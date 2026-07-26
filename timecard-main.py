import discord
from discord.ext import commands
import datetime
import aiosqlite
import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# 環境変数からデータベースディレクトリとDiscordトークンを取得
DB_DIR = os.getenv('DB_DIR')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

def validate_config():
    if not DB_DIR:
        raise ValueError('DB_DIR 環境変数が設定されていません。.env ファイルを確認してください。')
    if not DISCORD_TOKEN:
        raise ValueError('DISCORD_TOKEN 環境変数が設定されていません。.env ファイルを確認してください。')
    os.makedirs(DB_DIR, exist_ok=True)

validate_config()

ACTIVE_DB_PATH = os.path.join(DB_DIR, 'active_sessions.db')

def require_guild(ctx):
    if ctx.guild is None:
        return None
    return ctx.guild.id

# Intentsを設定
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True

# Botの初期化
bot = discord.Bot(intents=intents)

# 月オフセットから年・月キー（YYYY_MM）を取得する関数
def get_month_key(month_offset=0):
    today = datetime.datetime.now()
    year = today.year
    month = today.month + month_offset
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return f'{year}_{month:02d}'

def get_month_key_from_date(dt):
    if isinstance(dt, datetime.datetime):
        return dt.strftime('%Y_%m')
    return dt.strftime('%Y_%m')

def end_of_month(dt):
    """指定日の属する月の最終秒を返す（12月→1月も正しく処理）"""
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime.combine(dt, datetime.time.min)
    if dt.month == 12:
        return datetime.datetime(dt.year + 1, 1, 1) - datetime.timedelta(seconds=1)
    return datetime.datetime(dt.year, dt.month + 1, 1) - datetime.timedelta(seconds=1)

def start_of_month(dt):
    """指定日の属する月の開始日時を返す"""
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        return datetime.datetime.combine(dt.replace(day=1), datetime.time.min)
    return datetime.datetime(dt.year, dt.month, 1)

def get_db_path_for_date(dt):
    return os.path.join(DB_DIR, f'work_tracking_{get_month_key_from_date(dt)}.db')

def overlap_seconds(range_start, range_end, break_start, break_end):
    overlap_start = max(range_start, break_start)
    overlap_end = min(range_end, break_end)
    if overlap_start >= overlap_end:
        return 0
    return (overlap_end - overlap_start).total_seconds()

def calculate_break_in_range(range_start, range_end, breaks):
    return sum(
        overlap_seconds(range_start, range_end, break_start, break_end)
        for break_start, break_end in breaks
    )

HISTORY_TABLE_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER,
        user_id INTEGER,
        start_time TEXT,
        end_time TEXT,
        total_break_duration REAL,
        work_duration REAL
    )
'''

async def get_monthly_table_for_date(dt):
    db_path = get_db_path_for_date(dt)
    table_name = f"history_{get_month_key_from_date(dt)}"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(HISTORY_TABLE_SCHEMA.format(table_name=table_name))
        await conn.commit()
    return table_name, db_path

# 年と月ごとにデータベースファイルのパスを取得する関数
def get_db_path(month_offset=0):
    db_path = os.path.join(DB_DIR, f'work_tracking_{get_month_key(month_offset)}.db')
    return db_path

# 出勤中セッション用DBの初期化（月をまたいでも退勤可能）
async def init_active_db():
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                start_time TEXT,
                is_on_break INTEGER,
                break_start_time TEXT,
                total_break_duration REAL,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS break_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                break_start TEXT NOT NULL,
                break_end TEXT
            )
        ''')
        await conn.commit()

async def get_session_breaks(guild_id, user_id, session_start):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        async with conn.execute('''
            SELECT break_start, break_end FROM break_records
            WHERE guild_id = ? AND user_id = ? AND break_start >= ? AND break_end IS NOT NULL
        ''', (guild_id, user_id, session_start)) as cursor:
            rows = await cursor.fetchall()
    breaks = []
    for break_start_str, break_end_str in rows:
        breaks.append((
            datetime.datetime.strptime(break_start_str, '%Y-%m-%d %H:%M:%S'),
            datetime.datetime.strptime(break_end_str, '%Y-%m-%d %H:%M:%S'),
        ))
    return breaks

async def delete_session_breaks(guild_id, user_id, session_start):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('''
            DELETE FROM break_records
            WHERE guild_id = ? AND user_id = ? AND break_start >= ?
        ''', (guild_id, user_id, session_start))
        await conn.commit()

# 月ごとのテーブルを動的に作成する関数
async def get_monthly_table(month_offset=0):
    db_path = get_db_path(month_offset)
    table_name = f"history_{get_month_key(month_offset)}"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(HISTORY_TABLE_SCHEMA.format(table_name=table_name))
        await conn.commit()
    return table_name

# スラッシュコマンドを同期する関数
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    try:
        await bot.sync_commands()
        print("スラッシュコマンドを同期しました")
    except Exception as e:
        print(f"スラッシュコマンドの同期中にエラーが発生しました: {e}")

# 出勤データを保存
async def save_start_time(guild_id, user_id, start_time):
    try:
        await init_active_db()
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('''
                INSERT OR REPLACE INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, ?, 0, 0)
            ''', (guild_id, user_id, start_time))
            await conn.commit()
    except Exception as e:
        print(f"Error saving start time: {e}")

# 出勤コマンド
@bot.slash_command(name="start", description="出勤時に使うコマンド。出勤時間を記録します。")
async def start(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        await init_active_db()
        user_id = ctx.author.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute(
                'SELECT start_time, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ) as cursor:
                result = await cursor.fetchone()

            if result:
                await conn.rollback()
                if result[1] == 1:
                    await ctx.respond(f"{ctx.author.mention} さん、休憩中のため出勤できません。まずは /restart コマンドで休憩を終了してください。")
                else:
                    await ctx.respond(f"{ctx.author.mention} さん、既に出勤しています。")
                return

            start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            await conn.execute('''
                INSERT INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, ?, 0, 0)
            ''', (guild_id, user_id, start_time))
            await conn.commit()

        await ctx.respond(f"{ctx.author.mention} さん、{start_time} に出勤しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 退勤データを保存（動的な月テーブルに記録）
async def save_work_history(guild_id, user_id, start_time, end_time, breaks):
    try:
        start_date = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').date()
        end_date = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').date()
        start_dt = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
        total_break = calculate_break_in_range(start_dt, end_dt, breaks)

        if start_date.year != end_date.year or start_date.month != end_date.month:
            end_of_start_month = end_of_month(start_date)
            start_of_end_month = start_of_month(end_date)

            break_first = calculate_break_in_range(start_dt, end_of_start_month, breaks)
            break_second = calculate_break_in_range(start_of_end_month, end_dt, breaks)
            work_duration_first_month = (end_of_start_month - start_dt).total_seconds() - break_first
            work_duration_second_month = (end_dt - start_of_end_month).total_seconds() - break_second
            table_name_first_month, db_path_first_month = await get_monthly_table_for_date(start_date)
            table_name_second_month, db_path_second_month = await get_monthly_table_for_date(end_date)

            async with aiosqlite.connect(db_path_first_month) as conn:
                await conn.execute(f'''
                    INSERT INTO {table_name_first_month} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_time, end_of_start_month.strftime('%Y-%m-%d %H:%M:%S'), break_first, work_duration_first_month))
                await conn.commit()

            async with aiosqlite.connect(db_path_second_month) as conn:
                await conn.execute(f'''
                    INSERT INTO {table_name_second_month} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_of_end_month.strftime('%Y-%m-%d %H:%M:%S'), end_time, break_second, work_duration_second_month))
                await conn.commit()
        else:
            work_duration = (end_dt - start_dt).total_seconds() - total_break
            table_name = await get_monthly_table()
            db_path = get_db_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute(f'''
                    INSERT INTO {table_name} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_time, end_time, total_break, work_duration))
                await conn.commit()
    except Exception as e:
        print(f"Error saving work history: {e}")

# 退勤コマンド
@bot.slash_command(name="end", description="退勤時に使うコマンド。退勤時間を記録し、勤務時間を表示します。")
async def end(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        await init_active_db()
        user_id = ctx.author.id

        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute(
                'SELECT start_time, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ) as cursor:
                result = await cursor.fetchone()

            if not result:
                await conn.rollback()
                await ctx.respond(f"{ctx.author.mention} さん、まだ出勤していません。/start を使用してください。")
                return

            if result[1] == 1:
                await conn.rollback()
                await ctx.respond(f"{ctx.author.mention} さん、休憩中のため退勤できません。まずは /restart コマンドで休憩を終了してください。")
                return

            session_start_str = result[0]
            start_time = datetime.datetime.strptime(session_start_str, '%Y-%m-%d %H:%M:%S')
            end_time = datetime.datetime.now()
            breaks = await get_session_breaks(guild_id, user_id, session_start_str)
            break_total = calculate_break_in_range(start_time, end_time, breaks)
            work_duration = (end_time - start_time).total_seconds() - break_total

            await conn.execute('DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            await conn.commit()

        await save_work_history(guild_id, user_id, session_start_str, end_time.strftime('%Y-%m-%d %H:%M:%S'), breaks)
        await delete_session_breaks(guild_id, user_id, session_start_str)

            hours, remainder = divmod(work_duration, 3600)
            minutes = remainder // 60
            await ctx.respond(f"{ctx.author.mention} さん、退勤しました。勤務時間は {int(hours)}時間{int(minutes)}分です。")

    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩開始のデータを保存
async def save_break_time(guild_id, user_id, break_start_time):
    try:
        await init_active_db()
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute(
                'UPDATE active_sessions SET is_on_break = 1, break_start_time = ? WHERE guild_id = ? AND user_id = ?',
                (break_start_time, guild_id, user_id)
            )
            await conn.execute(
                'INSERT INTO break_records (guild_id, user_id, break_start) VALUES (?, ?, ?)',
                (guild_id, user_id, break_start_time)
            )
            await conn.commit()
    except Exception as e:
        print(f"Error saving break time: {e}")

# 休憩開始コマンド
@bot.slash_command(name="break", description="休憩を開始するコマンド。休憩時間を記録します。")
async def break_(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        await init_active_db()
        user_id = ctx.author.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute(
                'SELECT start_time, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?',
                (guild_id, user_id)
            ) as cursor:
                result = await cursor.fetchone()

            if not result or result[0] is None:
                await conn.rollback()
                await ctx.respond(f"{ctx.author.mention} さん、まずは /start で出勤してください。")
            elif result[1] == 1:
                await conn.rollback()
                await ctx.respond(f"{ctx.author.mention} さんは既に休憩中です。")
            else:
                break_start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                await conn.execute(
                    'UPDATE active_sessions SET is_on_break = 1, break_start_time = ? WHERE guild_id = ? AND user_id = ?',
                    (break_start_time, guild_id, user_id)
                )
                await conn.execute(
                    'INSERT INTO break_records (guild_id, user_id, break_start) VALUES (?, ?, ?)',
                    (guild_id, user_id, break_start_time)
                )
                await conn.commit()
                await ctx.respond(f"{ctx.author.mention} さん、{break_start_time} に休憩を開始しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩終了時に休憩時間を更新
async def finish_break(guild_id, user_id, break_end_time):
    try:
        await init_active_db()
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('''
                UPDATE active_sessions
                SET is_on_break = 0
                WHERE guild_id = ? AND user_id = ?
            ''', (guild_id, user_id))
            await conn.execute('''
                UPDATE break_records
                SET break_end = ?
                WHERE id = (
                    SELECT id FROM break_records
                    WHERE guild_id = ? AND user_id = ? AND break_end IS NULL
                    ORDER BY id DESC LIMIT 1
                )
            ''', (break_end_time, guild_id, user_id))
            await conn.commit()
    except Exception as e:
        print(f"Error updating break duration: {e}")

# 休憩終了コマンド
@bot.slash_command(name="restart", description="休憩を終了するコマンド。累積休憩時間に休憩時間を追加します。")
async def restart(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        await init_active_db()
        user_id = ctx.author.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            async with conn.execute(
                'SELECT break_start_time FROM active_sessions WHERE guild_id = ? AND user_id = ? AND is_on_break = 1',
                (guild_id, user_id)
            ) as cursor:
                result = await cursor.fetchone()

            if not result:
                await conn.rollback()
                await ctx.respond(f"{ctx.author.mention} さん、休憩中ではありません。/break で休憩を開始してください。")
            else:
                break_end = datetime.datetime.now()
                break_end_time = break_end.strftime('%Y-%m-%d %H:%M:%S')
                await conn.execute('''
                    UPDATE active_sessions SET is_on_break = 0
                    WHERE guild_id = ? AND user_id = ?
                ''', (guild_id, user_id))
                await conn.execute('''
                    UPDATE break_records SET break_end = ?
                    WHERE id = (
                        SELECT id FROM break_records
                        WHERE guild_id = ? AND user_id = ? AND break_end IS NULL
                        ORDER BY id DESC LIMIT 1
                    )
                ''', (break_end_time, guild_id, user_id))
                await conn.commit()
                await ctx.respond(f"{ctx.author.mention} さん、{break_end.strftime('%H:%M')} に休憩を終了しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 月ごとの勤務時間を表示するコマンド
@bot.slash_command(name="monthly", description="今月の合計勤務時間を表示するコマンドです。")
async def monthly(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        user_id = ctx.author.id
        db_path = get_db_path()
        table_name = await get_monthly_table()
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE guild_id = ? AND user_id = ?
            ''', (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
                total_seconds = row[0]

        if total_seconds:
            hours, remainder = divmod(total_seconds, 3600)
            minutes = remainder // 60
            await ctx.respond(f"{ctx.author.mention} さんの今月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。")
        else:
            await ctx.respond(f"{ctx.author.mention} さん、今月の勤務履歴はありません。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 先月の勤務時間を表示するコマンド
@bot.slash_command(name="last_monthly", description="先月の合計勤務時間を表示するコマンドです。")
async def last_monthly(ctx):
    try:
        guild_id = require_guild(ctx)
        if guild_id is None:
            await ctx.respond("このコマンドはサーバー内でのみ使用できます。")
            return

        user_id = ctx.author.id
        table_name = f"history_{get_month_key(month_offset=-1)}"
        db_path = get_db_path(month_offset=-1)

        if not os.path.exists(db_path):
            await ctx.respond(f"{ctx.author.mention} さん、先月の勤務履歴はありません。")
            return

        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ) as cursor:
                table_exists = await cursor.fetchone()

            if not table_exists:
                await ctx.respond(f"{ctx.author.mention} さん、先月の勤務履歴はありません。")
                return

            async with conn.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE guild_id = ? AND user_id = ?
            ''', (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
                total_seconds = row[0]

        if total_seconds:
            hours, remainder = divmod(total_seconds, 3600)
            minutes = remainder // 60
            await ctx.respond(f"{ctx.author.mention} さんの先月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。")
        else:
            await ctx.respond(f"{ctx.author.mention} さん、先月の勤務履歴はありません。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# Botを実行
bot.run(DISCORD_TOKEN)
