import discord
from discord.ext import commands
import datetime
import aiosqlite
import os
import logging
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = 'エラーが発生しました。しばらくしてから再度お試しください。'

load_dotenv()

DB_DIR = os.getenv('DB_DIR')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
_attendance_channel_id = os.getenv('ATTENDANCE_CHANNEL_ID')
ATTENDANCE_CHANNEL_ID = int(_attendance_channel_id) if _attendance_channel_id else None
PANEL_MESSAGE_SETTING_KEY = 'attendance_panel_message_id'
EMBED_COLOR = 0xF1C40F

def create_panel_embed() -> discord.Embed:
    return discord.Embed(title='操作パネル', color=EMBED_COLOR)

def validate_config():
    if not DB_DIR:
        raise ValueError('DB_DIR 環境変数が設定されていません。.env ファイルを確認してください。')
    if not DISCORD_TOKEN:
        raise ValueError('DISCORD_TOKEN 環境変数が設定されていません。.env ファイルを確認してください。')
    os.makedirs(DB_DIR, exist_ok=True)

validate_config()

ACTIVE_DB_PATH = os.path.join(DB_DIR, 'active_sessions.db')
LEGACY_GUILD_ID = 0  # 旧DB（guild_id なし）から移行したデータ用

def require_guild(interaction):
    if interaction.guild is None:
        return None
    return interaction.guild.id

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

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
    if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
        dt = datetime.datetime.combine(dt, datetime.time.min)
    if dt.month == 12:
        return datetime.datetime(dt.year + 1, 1, 1) - datetime.timedelta(seconds=1)
    return datetime.datetime(dt.year, dt.month + 1, 1) - datetime.timedelta(seconds=1)

def start_of_month(dt):
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

async def ensure_history_schema(conn, table_name):
    async with conn.execute(f'PRAGMA table_info({table_name})') as cursor:
        rows = await cursor.fetchall()
    columns = {row[1] for row in rows}
    if 'guild_id' not in columns:
        await conn.execute(f'ALTER TABLE {table_name} ADD COLUMN guild_id INTEGER')

async def get_monthly_table_for_date(dt):
    db_path = get_db_path_for_date(dt)
    table_name = f"history_{get_month_key_from_date(dt)}"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(HISTORY_TABLE_SCHEMA.format(table_name=table_name))
        await ensure_history_schema(conn, table_name)
        await conn.commit()
    return table_name, db_path

def get_db_path(month_offset=0):
    return os.path.join(DB_DIR, f'work_tracking_{get_month_key(month_offset)}.db')

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
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        await conn.commit()

async def get_session_breaks(guild_id, user_id, session_start):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        async with conn.execute('''
            SELECT break_start, break_end FROM break_records
            WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)
              AND break_start >= ? AND break_end IS NOT NULL
        ''', (user_id, guild_id, LEGACY_GUILD_ID, session_start)) as cursor:
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
            WHERE user_id = ? AND (guild_id = ? OR guild_id = ?) AND break_start >= ?
        ''', (user_id, guild_id, LEGACY_GUILD_ID, session_start))
        await conn.commit()

async def get_monthly_table(month_offset=0):
    db_path = get_db_path(month_offset)
    table_name = f"history_{get_month_key(month_offset)}"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(HISTORY_TABLE_SCHEMA.format(table_name=table_name))
        await ensure_history_schema(conn, table_name)
        await conn.commit()
    return table_name

async def migrate_legacy_users_to_active_sessions():
    """月別DBの users テーブルから active_sessions.db へ出勤中データを移行"""
    if not os.path.isdir(DB_DIR):
        return

    legacy_sessions = {}
    for filename in os.listdir(DB_DIR):
        if not filename.startswith('work_tracking_') or not filename.endswith('.db'):
            continue
        db_path = os.path.join(DB_DIR, filename)
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ) as cursor:
                if not await cursor.fetchone():
                    continue
            async with conn.execute(
                'SELECT id, start_time, is_on_break, break_start_time, total_break_duration FROM users'
            ) as cursor:
                rows = await cursor.fetchall()
        for user_id, start_time, is_on_break, break_start_time, total_break_duration in rows:
            if not start_time:
                continue
            existing = legacy_sessions.get(user_id)
            if existing is None or start_time > existing[0]:
                legacy_sessions[user_id] = (
                    start_time, is_on_break, break_start_time, total_break_duration or 0
                )

    if not legacy_sessions:
        return

    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        for user_id, (start_time, is_on_break, break_start_time, total_break_duration) in legacy_sessions.items():
            async with conn.execute(
                'SELECT 1 FROM active_sessions WHERE user_id = ?',
                (user_id,)
            ) as cursor:
                if await cursor.fetchone():
                    continue
            await conn.execute('''
                INSERT INTO active_sessions (guild_id, user_id, start_time, is_on_break, break_start_time, total_break_duration)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (LEGACY_GUILD_ID, user_id, start_time, is_on_break or 0, break_start_time, total_break_duration))
            if is_on_break and break_start_time:
                await conn.execute('''
                    INSERT INTO break_records (guild_id, user_id, break_start)
                    VALUES (?, ?, ?)
                ''', (LEGACY_GUILD_ID, user_id, break_start_time))
        await conn.commit()
    logger.info('Migrated %d legacy active session(s) from monthly DBs', len(legacy_sessions))

async def migrate_legacy_history_tables():
    """既存の history テーブルに guild_id カラムを追加"""
    if not os.path.isdir(DB_DIR):
        return

    for filename in os.listdir(DB_DIR):
        if not filename.startswith('work_tracking_') or not filename.endswith('.db'):
            continue
        db_path = os.path.join(DB_DIR, filename)
        month_key = filename.removeprefix('work_tracking_').removesuffix('.db')
        table_name = f'history_{month_key}'
        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ) as cursor:
                if not await cursor.fetchone():
                    continue
            await ensure_history_schema(conn, table_name)
            await conn.commit()

async def get_assigned_legacy_guild():
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        async with conn.execute(
            "SELECT value FROM app_settings WHERE key = 'legacy_guild_id'"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else None

async def assign_all_legacy_data_to_guild(guild_id):
    """初回コマンド実行時、旧DBの全データをそのサーバーIDに一括紐付け"""
    if guild_id == LEGACY_GUILD_ID:
        return
    if await get_assigned_legacy_guild() is not None:
        return

    history_updated = 0
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute(
            'UPDATE active_sessions SET guild_id = ? WHERE guild_id = ?',
            (guild_id, LEGACY_GUILD_ID)
        )
        await conn.execute(
            'UPDATE break_records SET guild_id = ? WHERE guild_id = ?',
            (guild_id, LEGACY_GUILD_ID)
        )
        await conn.execute(
            "INSERT INTO app_settings (key, value) VALUES ('legacy_guild_id', ?)",
            (str(guild_id),)
        )
        await conn.commit()

    if os.path.isdir(DB_DIR):
        for filename in os.listdir(DB_DIR):
            if not filename.startswith('work_tracking_') or not filename.endswith('.db'):
                continue
            db_path = os.path.join(DB_DIR, filename)
            month_key = filename.removeprefix('work_tracking_').removesuffix('.db')
            table_name = f'history_{month_key}'
            async with aiosqlite.connect(db_path) as conn:
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,)
                ) as cursor:
                    if not await cursor.fetchone():
                        continue
                await ensure_history_schema(conn, table_name)
                cursor = await conn.execute(
                    f'UPDATE {table_name} SET guild_id = ? WHERE guild_id IS NULL',
                    (guild_id,)
                )
                history_updated += cursor.rowcount
                await conn.commit()

    logger.info(
        'Assigned all legacy data to guild %s (%d history records updated)',
        guild_id, history_updated
    )

async def get_app_setting(key):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        async with conn.execute(
            'SELECT value FROM app_settings WHERE key = ?', (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def set_app_setting(key, value):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute(
            'INSERT INTO app_settings (key, value) VALUES (?, ?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            (key, str(value))
        )
        await conn.commit()

async def get_panel_message_id():
    value = await get_app_setting(PANEL_MESSAGE_SETTING_KEY)
    return int(value) if value else None

async def set_panel_message_id(message_id):
    await set_app_setting(PANEL_MESSAGE_SETTING_KEY, message_id)

async def ensure_guild_ready(guild_id):
    await init_active_db()
    await assign_all_legacy_data_to_guild(guild_id)

async def migrate_legacy_data():
    await init_active_db()
    await migrate_legacy_users_to_active_sessions()
    await migrate_legacy_history_tables()

async def fetch_active_session(conn, guild_id, user_id):
    async with conn.execute('''
        SELECT guild_id, start_time, is_on_break, break_start_time, total_break_duration
        FROM active_sessions
        WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)
        ORDER BY guild_id DESC
    ''', (user_id, guild_id, LEGACY_GUILD_ID)) as cursor:
        return await cursor.fetchone()

async def save_work_history(guild_id, user_id, start_time, end_time, breaks, legacy_break_total=0):
    try:
        start_date = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S').date()
        end_date = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S').date()
        start_dt = datetime.datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
        end_dt = datetime.datetime.strptime(end_time, '%Y-%m-%d %H:%M:%S')
        total_break = calculate_break_in_range(start_dt, end_dt, breaks)
        if total_break == 0 and legacy_break_total > 0:
            total_break = legacy_break_total

        if start_date.year != end_date.year or start_date.month != end_date.month:
            end_of_start_month = end_of_month(start_date)
            start_of_end_month = start_of_month(end_date)

            break_first = calculate_break_in_range(start_dt, end_of_start_month, breaks)
            break_second = calculate_break_in_range(start_of_end_month, end_dt, breaks)
            if break_first + break_second == 0 and legacy_break_total > 0:
                break_first = legacy_break_total
            work_duration_first_month = max(0, (end_of_start_month - start_dt).total_seconds() - break_first)
            work_duration_second_month = max(0, (end_dt - start_of_end_month).total_seconds() - break_second)
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
            work_duration = max(0, (end_dt - start_dt).total_seconds() - total_break)
            table_name = await get_monthly_table()
            db_path = get_db_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute(f'''
                    INSERT INTO {table_name} (guild_id, user_id, start_time, end_time, total_break_duration, work_duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (guild_id, user_id, start_time, end_time, total_break, work_duration))
                await conn.commit()
    except Exception:
        logger.exception('Failed to save work history')

async def action_start(guild_id, user_id, mention):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        result = await fetch_active_session(conn, guild_id, user_id)

        if result:
            await conn.rollback()
            if result[2] == 1:
                return f'{mention} さん、休憩中のため出勤できません。先に休憩を終了してください。'
            return f'{mention} さん、既に出勤しています。'

        start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        await conn.execute('''
            INSERT INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
            VALUES (?, ?, ?, 0, 0)
        ''', (guild_id, user_id, start_time))
        await conn.commit()

    return f'{mention} さん、{start_time} に出勤しました。'

async def action_end(guild_id, user_id, mention):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        result = await fetch_active_session(conn, guild_id, user_id)

        if not result:
            await conn.rollback()
            return f'{mention} さん、まだ出勤していません。'

        if result[2] == 1:
            await conn.rollback()
            return f'{mention} さん、休憩中のため退勤できません。先に休憩を終了してください。'

        session_start_str = result[1]
        legacy_break_total = result[4] or 0
        start_time = datetime.datetime.strptime(session_start_str, '%Y-%m-%d %H:%M:%S')
        end_time = datetime.datetime.now()
        breaks = await get_session_breaks(guild_id, user_id, session_start_str)
        break_total = calculate_break_in_range(start_time, end_time, breaks)
        if break_total == 0 and legacy_break_total > 0:
            break_total = legacy_break_total
        work_duration = max(0, (end_time - start_time).total_seconds() - break_total)

        await conn.execute(
            'DELETE FROM active_sessions WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)',
            (user_id, guild_id, LEGACY_GUILD_ID)
        )
        await conn.commit()

    await save_work_history(
        guild_id, user_id, session_start_str,
        end_time.strftime('%Y-%m-%d %H:%M:%S'), breaks, legacy_break_total
    )
    await delete_session_breaks(guild_id, user_id, session_start_str)

    hours, remainder = divmod(work_duration, 3600)
    minutes = remainder // 60
    return f'{mention} さん、退勤しました。勤務時間は {int(hours)}時間{int(minutes)}分です。'

async def action_break(guild_id, user_id, mention):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        result = await fetch_active_session(conn, guild_id, user_id)

        if not result or result[1] is None:
            await conn.rollback()
            return f'{mention} さん、まずは出勤してください。'

        if result[2] == 1:
            await conn.rollback()
            return f'{mention} さんは既に休憩中です。'

        break_start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        await conn.execute(
            'UPDATE active_sessions SET is_on_break = 1, break_start_time = ? WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)',
            (break_start_time, user_id, guild_id, LEGACY_GUILD_ID)
        )
        await conn.execute(
            'INSERT INTO break_records (guild_id, user_id, break_start) VALUES (?, ?, ?)',
            (guild_id, user_id, break_start_time)
        )
        await conn.commit()

    return f'{mention} さん、{break_start_time} に休憩を開始しました。'

async def action_restart(guild_id, user_id, mention):
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
        result = await fetch_active_session(conn, guild_id, user_id)

        if not result or result[2] != 1:
            await conn.rollback()
            return f'{mention} さん、休憩中ではありません。'

        break_end = datetime.datetime.now()
        break_end_time = break_end.strftime('%Y-%m-%d %H:%M:%S')
        await conn.execute('''
            UPDATE active_sessions SET is_on_break = 0
            WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)
        ''', (user_id, guild_id, LEGACY_GUILD_ID))
        await conn.execute('''
            UPDATE break_records SET break_end = ?
            WHERE id = (
                SELECT id FROM break_records
                WHERE user_id = ? AND (guild_id = ? OR guild_id = ?) AND break_end IS NULL
                ORDER BY id DESC LIMIT 1
            )
        ''', (break_end_time, user_id, guild_id, LEGACY_GUILD_ID))
        await conn.commit()

    return f'{mention} さん、{break_end.strftime("%H:%M")} に休憩を終了しました。'

async def action_monthly(guild_id, user_id, mention):
    db_path = get_db_path()
    table_name = await get_monthly_table()
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(f'''
            SELECT SUM(work_duration) FROM {table_name}
            WHERE user_id = ? AND guild_id = ?
        ''', (user_id, guild_id)) as cursor:
            row = await cursor.fetchone()
            total_seconds = row[0]

    if total_seconds:
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f'{mention} さんの今月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。'

    return f'{mention} さん、今月の勤務履歴はありません。'

async def action_last_monthly(guild_id, user_id, mention):
    table_name = f"history_{get_month_key(month_offset=-1)}"
    db_path = get_db_path(month_offset=-1)

    if not os.path.exists(db_path):
        return f'{mention} さん、先月の勤務履歴はありません。'

    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        ) as cursor:
            if not await cursor.fetchone():
                return f'{mention} さん、先月の勤務履歴はありません。'

        async with conn.execute(f'''
            SELECT SUM(work_duration) FROM {table_name}
            WHERE user_id = ? AND guild_id = ?
        ''', (user_id, guild_id)) as cursor:
            row = await cursor.fetchone()
            total_seconds = row[0]

    if total_seconds:
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        return f'{mention} さんの先月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。'

    return f'{mention} さん、先月の勤務履歴はありません。'

async def handle_attendance_button(interaction: discord.Interaction, action):
    if interaction.guild is None:
        await interaction.response.send_message(
            'この操作はサーバー内でのみ使用できます。',
            ephemeral=True
        )
        return

    if ATTENDANCE_CHANNEL_ID is None:
        await interaction.response.send_message(
            '出退勤チャンネルが設定されていません。',
            ephemeral=True
        )
        return

    if interaction.channel_id != ATTENDANCE_CHANNEL_ID:
        await interaction.response.send_message(
            '出退勤チャンネルで操作してください。',
            ephemeral=True
        )
        return

    try:
        guild_id = interaction.guild.id
        await ensure_guild_ready(guild_id)
        message = await action(guild_id, interaction.user.id, interaction.user.mention)
        await interaction.response.send_message(message)
        if isinstance(interaction.channel, discord.TextChannel):
            await refresh_attendance_panel(interaction.channel)
    except Exception:
        logger.exception('Button action failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

class AttendancePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label='出勤', style=discord.ButtonStyle.success,
        custom_id='attendance:start', row=0
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_start)

    @discord.ui.button(
        label='退勤', style=discord.ButtonStyle.danger,
        custom_id='attendance:end', row=0
    )
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_end)

    @discord.ui.button(
        label='休憩開始', style=discord.ButtonStyle.secondary,
        custom_id='attendance:break', row=0
    )
    async def break_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_break)

    @discord.ui.button(
        label='休憩終了', style=discord.ButtonStyle.primary,
        custom_id='attendance:restart', row=0
    )
    async def restart_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_restart)

    @discord.ui.button(
        label='今月', style=discord.ButtonStyle.secondary,
        custom_id='attendance:monthly', row=1
    )
    async def monthly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_monthly)

    @discord.ui.button(
        label='先月', style=discord.ButtonStyle.secondary,
        custom_id='attendance:last_monthly', row=1
    )
    async def last_monthly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_attendance_button(interaction, action_last_monthly)

async def refresh_attendance_panel(channel):
    old_id = await get_panel_message_id()
    if old_id:
        try:
            old_message = await channel.fetch_message(old_id)
            await old_message.delete()
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning('Cannot delete old attendance panel message %s', old_id)

    message = await channel.send(embed=create_panel_embed(), view=AttendancePanelView())
    await set_panel_message_id(message.id)

async def refresh_attendance_panel_if_needed(channel):
    if ATTENDANCE_CHANNEL_ID is None or channel.id != ATTENDANCE_CHANNEL_ID:
        return
    await refresh_attendance_panel(channel)

async def ensure_attendance_panel(channel):
    panel_id = await get_panel_message_id()
    if panel_id:
        try:
            msg = await channel.fetch_message(panel_id)
            if msg.embeds and msg.components:
                return
        except (discord.NotFound, discord.Forbidden):
            logger.info('Attendance panel message missing, reposting')

    await refresh_attendance_panel(channel)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    bot.add_view(AttendancePanelView())
    try:
        await migrate_legacy_data()
        print('旧DBの互換性チェック・移行が完了しました')
    except Exception:
        logger.exception('Failed to migrate legacy data')
    try:
        await bot.tree.sync()
        print('スラッシュコマンドを同期しました')
    except Exception:
        logger.exception('Failed to sync slash commands')
    if ATTENDANCE_CHANNEL_ID:
        channel = bot.get_channel(ATTENDANCE_CHANNEL_ID)
        if channel is None:
            logger.warning('ATTENDANCE_CHANNEL_ID %s not found', ATTENDANCE_CHANNEL_ID)
        else:
            try:
                await ensure_attendance_panel(channel)
                print(f'出退勤パネルを #{channel.name} に配置しました')
            except Exception:
                logger.exception('Failed to setup attendance panel')
    else:
        logger.info('ATTENDANCE_CHANNEL_ID is not set; button panel disabled')

async def run_slash_action(interaction, action):
    guild_id = require_guild(interaction)
    if guild_id is None:
        await interaction.response.send_message('このコマンドはサーバー内でのみ使用できます。')
        return

    await ensure_guild_ready(guild_id)
    message = await action(guild_id, interaction.user.id, interaction.user.mention)
    await interaction.response.send_message(message)
    if isinstance(interaction.channel, discord.TextChannel):
        await refresh_attendance_panel_if_needed(interaction.channel)

@bot.tree.command(name='start', description='出勤時に使うコマンド。出勤時間を記録します。')
async def start(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_start)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='end', description='退勤時に使うコマンド。退勤時間を記録し、勤務時間を表示します。')
async def end(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_end)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='break', description='休憩を開始するコマンド。休憩時間を記録します。')
async def break_(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_break)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='restart', description='休憩を終了するコマンド。累積休憩時間に休憩時間を追加します。')
async def restart(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_restart)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='monthly', description='今月の合計勤務時間を表示するコマンドです。')
async def monthly(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_monthly)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='last_monthly', description='先月の合計勤務時間を表示するコマンドです。')
async def last_monthly(interaction: discord.Interaction):
    try:
        await run_slash_action(interaction, action_last_monthly)
    except Exception:
        logger.exception('Command failed')
        if interaction.response.is_done():
            await interaction.followup.send(GENERIC_ERROR_MESSAGE)
        else:
            await interaction.response.send_message(GENERIC_ERROR_MESSAGE)

bot.run(DISCORD_TOKEN)
