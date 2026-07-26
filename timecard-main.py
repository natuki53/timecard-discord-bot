import discord
from discord.ext import commands
import datetime
import sqlite3
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

def get_monthly_table_for_date(dt):
    db_path = get_db_path_for_date(dt)
    table_name = f"history_{get_month_key_from_date(dt)}"
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute(f'''
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                start_time TEXT,
                end_time TEXT,
                total_break_duration REAL,
                work_duration REAL
            )
        ''')
        conn.commit()
    return table_name, db_path

# 年と月ごとにデータベースファイルのパスを取得する関数
def get_db_path(month_offset=0):
    db_path = os.path.join(DB_DIR, f'work_tracking_{get_month_key(month_offset)}.db')
    return db_path

# 出勤中セッション用DBの初期化（月をまたいでも退勤可能）
def init_active_db():
    with sqlite3.connect(ACTIVE_DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''
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
        conn.commit()

# 月ごとのテーブルを動的に作成する関数
def get_monthly_table(month_offset=0):
    db_path = get_db_path(month_offset)
    table_name = f"history_{get_month_key(month_offset)}"
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute(f'''
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                start_time TEXT,
                end_time TEXT,
                total_break_duration REAL,
                work_duration REAL
            )
        ''')
        conn.commit()
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
def save_start_time(guild_id, user_id, start_time):
    try:
        init_active_db()
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, ?, 0, 0)
            ''', (guild_id, user_id, start_time))
            conn.commit()
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

        init_active_db()
        user_id = ctx.author.id
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('SELECT start_time, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            result = c.fetchone()
            
            if result:
                if result[1] == 1:
                    await ctx.respond(f"{ctx.author.mention} さん、休憩中のため出勤できません。まずは /restart コマンドで休憩を終了してください。")
                else:
                    await ctx.respond(f"{ctx.author.mention} さん、既に出勤しています。")
                return

            start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                INSERT INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, ?, 0, 0)
            ''', (guild_id, user_id, start_time))
            conn.commit()
            
        await ctx.respond(f"{ctx.author.mention} さん、{start_time} に出勤しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 退勤データを保存（動的な月テーブルに記録）
def save_work_history(guild_id, user_id, start_time, end_time, break_duration, work_duration):
    try:
        start_date = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').date()
        end_date = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').date()

        if start_date.year != end_date.year or start_date.month != end_date.month:
            # 出勤時間と退勤時間が異なる月にまたがる場合
            end_of_start_month = end_of_month(start_date)
            start_of_end_month = start_of_month(end_date)

            start_dt = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
            end_dt = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')

            # 最初の月のレコード
            work_duration_first_month = (end_of_start_month - start_dt).total_seconds() - break_duration
            table_name_first_month, db_path_first_month = get_monthly_table_for_date(start_date)

            # 次の月のレコード
            work_duration_second_month = (end_dt - start_of_end_month).total_seconds()
            table_name_second_month, db_path_second_month = get_monthly_table_for_date(end_date)

            with sqlite3.connect(db_path_first_month) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name_first_month} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_time, end_of_start_month.strftime('%Y-%m-%d %H:%M:%S'), break_duration, work_duration_first_month))
                conn.commit()

            with sqlite3.connect(db_path_second_month) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name_second_month} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_of_end_month.strftime('%Y-%m-%d %H:%M:%S'), end_time, 0, work_duration_second_month))
                conn.commit()
        else:
            # 同じ月の場合
            table_name = get_monthly_table()
            db_path = get_db_path()
            with sqlite3.connect(db_path) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_time, end_time, break_duration, work_duration))
                conn.commit()
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

        init_active_db()
        user_id = ctx.author.id
        
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('SELECT start_time, total_break_duration, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            result = c.fetchone()
            
            # 出勤記録がない場合
            if not result:
                await ctx.respond(f"{ctx.author.mention} さん、まだ出勤していません。/start を使用してください。")
                return
                
            # 休憩中の場合
            if result[2] == 1:
                await ctx.respond(f"{ctx.author.mention} さん、休憩中のため退勤できません。まずは /restart コマンドで休憩を終了してください。")
                return
                
            # 退勤処理
            start_time = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
            end_time = datetime.datetime.now()
            work_duration = (end_time - start_time).total_seconds() - result[1]
            
            # 勤務履歴を保存
            save_work_history(guild_id, user_id, result[0], end_time.strftime('%Y-%m-%d %H:%M:%S'), result[1], work_duration)
            
            c.execute('DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            conn.commit()
            
            # 結果を表示
            hours, remainder = divmod(work_duration, 3600)
            minutes = remainder // 60
            await ctx.respond(f"{ctx.author.mention} さん、退勤しました。勤務時間は {int(hours)}時間{int(minutes)}分です。")
            
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩開始のデータを保存
def save_break_time(guild_id, user_id, break_start_time):
    try:
        init_active_db()
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('UPDATE active_sessions SET is_on_break = 1, break_start_time = ? WHERE guild_id = ? AND user_id = ?', (break_start_time, guild_id, user_id))
            conn.commit()
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

        init_active_db()
        user_id = ctx.author.id
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('SELECT start_time, is_on_break FROM active_sessions WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
            result = c.fetchone()

        if not result or result[0] is None:
            await ctx.respond(f"{ctx.author.mention} さん、まずは /start で出勤してください。")
        elif result[1] == 1:
            await ctx.respond(f"{ctx.author.mention} さんは既に休憩中です。")
        else:
            break_start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_break_time(guild_id, user_id, break_start_time)
            await ctx.respond(f"{ctx.author.mention} さん、{break_start_time} に休憩を開始しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩終了時に休憩時間を更新
def update_break_duration(guild_id, user_id, break_duration):
    try:
        init_active_db()
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''
                UPDATE active_sessions
                SET total_break_duration = total_break_duration + ?, is_on_break = 0
                WHERE guild_id = ? AND user_id = ?
            ''', (break_duration, guild_id, user_id))
            conn.commit()
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

        init_active_db()
        user_id = ctx.author.id
        with sqlite3.connect(ACTIVE_DB_PATH) as conn:
            c = conn.cursor()
            c.execute('SELECT break_start_time FROM active_sessions WHERE guild_id = ? AND user_id = ? AND is_on_break = 1', (guild_id, user_id))
            result = c.fetchone()

        if not result:
            await ctx.respond(f"{ctx.author.mention} さん、休憩中ではありません。/break で休憩を開始してください。")
        else:
            break_start = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
            break_end = datetime.datetime.now()
            break_duration = (break_end - break_start).total_seconds()
            update_break_duration(guild_id, user_id, break_duration)
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
        table_name = get_monthly_table()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE guild_id = ? AND user_id = ?
            ''', (guild_id, user_id))
            total_seconds = c.fetchone()[0]

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

        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            
            # 先月のテーブルが存在するか確認
            c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
            table_exists = c.fetchone()
            
            if not table_exists:
                await ctx.respond(f"{ctx.author.mention} さん、先月の勤務履歴はありません。")
                return
            
            # 先月の勤務時間の合計を取得
            c.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE guild_id = ? AND user_id = ?
            ''', (guild_id, user_id))
            total_seconds = c.fetchone()[0]
        
        # 結果を表示
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