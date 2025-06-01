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

# Intentsを設定
intents = discord.Intents.default()
intents.message_content = True
intents.presences = True
intents.members = True

# Botの初期化
bot = discord.Bot(intents=intents)

# 年と月ごとにデータベースファイルのパスを取得する関数
def get_db_path(month_offset=0):
    current_month = (datetime.datetime.now() + datetime.timedelta(days=month_offset * 30)).strftime('%Y_%m')
    db_path = os.path.join(DB_DIR, f'work_tracking_{current_month}.db')
    return db_path

# データベースとテーブルの初期化関数
def init_db(month_offset=0):
    db_path = get_db_path(month_offset)
    if not os.path.exists(db_path):
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    start_time TEXT,
                    is_on_break INTEGER,
                    break_start_time TEXT,
                    total_break_duration REAL
                )
            ''')
            conn.commit()

# 月ごとのテーブルを動的に作成する関数
def get_monthly_table(month_offset=0):
    db_path = get_db_path(month_offset)
    current_month = (datetime.datetime.now() + datetime.timedelta(days=month_offset * 30)).strftime('%Y_%m')
    table_name = f"history_{current_month}"
    with sqlite3.connect(db_path) as conn:
        c = conn.cursor()
        c.execute(f'''
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
def save_start_time(user_id, start_time):
    try:
        init_db()  # データベースを初期化
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('''
                INSERT OR REPLACE INTO users (id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, 0, 0)
            ''', (user_id, start_time))
            conn.commit()
    except Exception as e:
        print(f"Error saving start time: {e}")

# 出勤コマンド
@bot.slash_command(name="start", description="出勤時に使うコマンド。出勤時間を記録します。")
async def start(ctx):
    try:
        init_db()
        user_id = ctx.author.id
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('SELECT start_time, is_on_break FROM users WHERE id = ?', (user_id,))
            result = c.fetchone()
            
            if result:
                if result[1] == 1:
                    await ctx.respond(f"{ctx.author.mention} さん、休憩中のため出勤できません。まずは /restart コマンドで休憩を終了してください。")
                else:
                    await ctx.respond(f"{ctx.author.mention} さん、既に出勤しています。")
                return

            # 出勤を記録
            start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                INSERT INTO users (id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, 0, 0)
            ''', (user_id, start_time))
            conn.commit()
            
        await ctx.respond(f"{ctx.author.mention} さん、{start_time} に出勤しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 退勤データを保存（動的な月テーブルに記録）
def save_work_history(user_id, start_time, end_time, break_duration, work_duration):
    try:
        start_date = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').date()
        end_date = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').date()

        if start_date.month != end_date.month:
            # 出勤時間と退勤時間が異なる月にまたがる場合
            end_of_start_month = datetime.datetime(start_date.year, start_date.month + 1, 1) - datetime.timedelta(seconds=1)
            start_of_end_month = datetime.datetime(end_date.year, end_date.month, 1)

            # 最初の月のレコード
            work_duration_first_month = (end_of_start_month - datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')).total_seconds() - break_duration
            table_name_first_month = get_monthly_table()
            db_path_first_month = get_db_path()

            # 次の月のレコード
            work_duration_second_month = (datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S') - start_of_end_month).total_seconds()
            table_name_second_month = get_monthly_table(month_offset=1)
            db_path_second_month = get_db_path(month_offset=1)

            with sqlite3.connect(db_path_first_month) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name_first_month} (user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, start_time, end_of_start_month.strftime('%Y-%m-%d %H:%M:%S'), break_duration, work_duration_first_month))
                conn.commit()

            with sqlite3.connect(db_path_second_month) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name_second_month} (user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, start_of_end_month.strftime('%Y-%m-%d %H:%M:%S'), end_time, 0, work_duration_second_month))
                conn.commit()
        else:
            # 同じ月の場合
            table_name = get_monthly_table()
            db_path = get_db_path()
            with sqlite3.connect(db_path) as conn:
                c = conn.cursor()
                c.execute(f'''
                    INSERT INTO {table_name} (user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, start_time, end_time, break_duration, work_duration))
                conn.commit()
    except Exception as e:
        print(f"Error saving work history: {e}")

# 退勤コマンド
@bot.slash_command(name="end", description="退勤時に使うコマンド。退勤時間を記録し、勤務時間を表示します。")
async def end(ctx):
    try:
        init_db()
        user_id = ctx.author.id
        db_path = get_db_path()
        
        # データベース接続
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            
            # ユーザーの出勤記録を確認
            c.execute('SELECT start_time, total_break_duration, is_on_break FROM users WHERE id = ?', (user_id,))
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
            save_work_history(user_id, result[0], end_time.strftime('%Y-%m-%d %H:%M:%S'), result[1], work_duration)
            
            # ユーザーデータを削除
            c.execute('DELETE FROM users WHERE id = ?', (user_id,))
            conn.commit()
            
            # 結果を表示
            hours, remainder = divmod(work_duration, 3600)
            minutes = remainder // 60
            await ctx.respond(f"{ctx.author.mention} さん、退勤しました。勤務時間は {int(hours)}時間{int(minutes)}分です。")
            
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩開始のデータを保存
def save_break_time(user_id, break_start_time):
    try:
        init_db()  # データベースを初期化
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('UPDATE users SET is_on_break = 1, break_start_time = ? WHERE id = ?', (break_start_time, user_id))
            conn.commit()
    except Exception as e:
        print(f"Error saving break time: {e}")

# 休憩開始コマンド
@bot.slash_command(name="break", description="休憩を開始するコマンド。休憩時間を記録します。")
async def break_(ctx):
    try:
        init_db()
        user_id = ctx.author.id
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('SELECT start_time, is_on_break FROM users WHERE id = ?', (user_id,))
            result = c.fetchone()

        if not result or result[0] is None:
            await ctx.respond(f"{ctx.author.mention} さん、まずは /start で出勤してください。")
        elif result[1] == 1:
            await ctx.respond(f"{ctx.author.mention} さんは既に休憩中です。")
        else:
            break_start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            save_break_time(user_id, break_start_time)
            await ctx.respond(f"{ctx.author.mention} さん、{break_start_time} に休憩を開始しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 休憩終了時に休憩時間を更新
def update_break_duration(user_id, break_duration):
    try:
        init_db()  # データベースを初期化
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('''
                UPDATE users
                SET total_break_duration = total_break_duration + ?, is_on_break = 0
                WHERE id = ?
            ''', (break_duration, user_id))
            conn.commit()
    except Exception as e:
        print(f"Error updating break duration: {e}")

# 休憩終了コマンド
@bot.slash_command(name="restart", description="休憩を終了するコマンド。累積休憩時間に休憩時間を追加します。")
async def restart(ctx):
    try:
        init_db()
        user_id = ctx.author.id
        db_path = get_db_path()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute('SELECT break_start_time FROM users WHERE id = ? AND is_on_break = 1', (user_id,))
            result = c.fetchone()

        if not result:
            await ctx.respond(f"{ctx.author.mention} さん、休憩中ではありません。/break で休憩を開始してください。")
        else:
            break_start = datetime.datetime.strptime(result[0], '%Y-%m-%d %H:%M:%S')
            break_end = datetime.datetime.now()
            break_duration = (break_end - break_start).total_seconds()
            update_break_duration(user_id, break_duration)
            await ctx.respond(f"{ctx.author.mention} さん、{break_end.strftime('%H:%M')} に休憩を終了しました。")
    except Exception as e:
        await ctx.respond(f"エラーが発生しました: {e}")

# 月ごとの勤務時間を表示するコマンド
@bot.slash_command(name="monthly", description="今月の合計勤務時間を表示するコマンドです。")
async def monthly(ctx):
    try:
        init_db()
        user_id = ctx.author.id
        db_path = get_db_path()
        table_name = get_monthly_table()
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE user_id = ?
            ''', (user_id,))
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
        user_id = ctx.author.id
        
        # 先月の年と月を取得
        today = datetime.datetime.now()
        first_day_of_this_month = today.replace(day=1)
        last_month = first_day_of_this_month - datetime.timedelta(days=1)
        last_month_str = last_month.strftime('%Y_%m')
        
        # 先月のテーブル名を設定
        table_name = f"history_{last_month_str}"
        
        # 先月のデータベースパスを取得
        db_path = get_db_path()
        
        # データベース接続
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
                WHERE user_id = ?
            ''', (user_id,))
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