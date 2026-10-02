import discord
from discord.ext import commands
import asyncio
import aiosqlite
import os
import logging
from dotenv import load_dotenv

from bot_status import BotStatusReporter
from monthly_history import (
    JST,
    close_finished_months,
    finish_session,
    init_monthly_history_schema,
    next_month_start,
    now_jst,
)
from legacy_migration import migrate_legacy_users_once
from member_directory import (
    init_member_directory,
    list_unresolved_member_ids,
    upsert_member,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GENERIC_ERROR_MESSAGE = 'エラーが発生しました。しばらくしてから再度お試しください。'

load_dotenv()

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
LEGACY_GUILD_ID = 0  # 旧DB（guild_id なし）から移行したデータ用

def require_guild(interaction):
    if interaction.guild is None:
        return None
    return interaction.guild.id

async def acknowledge_interaction(interaction):
    """Discordの3秒制限より前にコマンド受信を確定する。"""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.NotFound:
        logger.warning(
            'Interaction %s expired before it could be acknowledged',
            interaction.id,
        )
    except discord.HTTPException:
        logger.exception(
            'Failed to acknowledge interaction %s',
            interaction.id,
        )
    return False

async def send_interaction_message(interaction, message):
    """応答済み・応答待ちのどちらでも安全にメッセージを返す。"""
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=message)
        else:
            await interaction.response.send_message(message)
        return True
    except discord.NotFound:
        logger.warning(
            'Interaction %s expired before a response could be delivered',
            interaction.id,
        )
    except discord.HTTPException:
        logger.exception(
            'Failed to respond to interaction %s',
            interaction.id,
        )
    return False

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
status_reporter = BotStatusReporter(
    bot_id='timecard',
    discord_connected=lambda: bot.is_ready() and not bot.is_closed(),
    gateway_latency_ms=lambda: bot.latency * 1_000,
)

def get_month_key(month_offset=0):
    today = now_jst()
    year = today.year
    month = today.month + month_offset
    while month > 12:
        month -= 12
        year += 1
    while month < 1:
        month += 12
        year -= 1
    return f'{year}_{month:02d}'

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

def get_db_path(month_offset=0):
    return os.path.join(DB_DIR, f'work_tracking_{get_month_key(month_offset)}.db')

async def init_active_db():
    async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
        await conn.execute('BEGIN IMMEDIATE')
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
        await init_monthly_history_schema(conn)
        await conn.commit()
    await init_member_directory(ACTIVE_DB_PATH)

async def remember_interaction_member(interaction):
    if interaction.guild_id is None:
        return
    await upsert_member(
        ACTIVE_DB_PATH,
        interaction.guild_id,
        interaction.user.id,
        interaction.user.display_name,
    )

async def backfill_member_directory():
    unresolved = await list_unresolved_member_ids(DB_DIR, ACTIVE_DB_PATH)
    guilds = {guild.id: guild for guild in bot.guilds}
    updated = 0
    for guild_id, user_id in unresolved:
        guild = guilds.get(guild_id)
        if guild is None:
            continue
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden):
                continue
            except discord.HTTPException:
                logger.warning(
                    "Failed to resolve Discord member %s in guild %s",
                    user_id,
                    guild_id,
                )
                continue
        await upsert_member(
            ACTIVE_DB_PATH,
            guild_id,
            user_id,
            member.display_name,
        )
        updated += 1
    if updated:
        logger.info("Backfilled %d Timecard member name(s)", updated)

async def get_monthly_table(month_offset=0):
    db_path = get_db_path(month_offset)
    table_name = f"history_{get_month_key(month_offset)}"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(HISTORY_TABLE_SCHEMA.format(table_name=table_name))
        await ensure_history_schema(conn, table_name)
        await conn.commit()
    return table_name

async def migrate_legacy_users_to_active_sessions():
    """月別DBの users テーブルを初回だけ active_sessions.db へ移行"""
    result = await migrate_legacy_users_once(
        DB_DIR,
        ACTIVE_DB_PATH,
        legacy_guild_id=LEGACY_GUILD_ID,
    )
    logger.info(
        'Legacy active-session migration: %s (%d imported)',
        result['status'],
        result['migrated'],
    )

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
            'UPDATE history_outbox SET guild_id = ? WHERE guild_id = ?',
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

async def ensure_guild_ready(guild_id):
    await init_active_db()
    await assign_all_legacy_data_to_guild(guild_id)
    await close_finished_months(DB_DIR, ACTIVE_DB_PATH)

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

monthly_close_task = None

async def monthly_close():
    while not bot.is_closed():
        deadline = next_month_start(now_jst()).replace(tzinfo=JST)
        await discord.utils.sleep_until(deadline)
        try:
            await close_finished_months(DB_DIR, ACTIVE_DB_PATH)
        except Exception:
            logger.exception('Failed to close completed work months; pending data retained')

def start_monthly_close():
    global monthly_close_task
    if monthly_close_task is None or monthly_close_task.done():
        monthly_close_task = asyncio.create_task(monthly_close())

@bot.event
async def on_ready():
    status_reporter.start()
    print(f'Logged in as {bot.user}')
    try:
        await migrate_legacy_data()
        print('旧DBの互換性チェック・移行が完了しました')
        start_monthly_close()
        await close_finished_months(DB_DIR, ACTIVE_DB_PATH)
        await backfill_member_directory()
    except Exception:
        logger.exception('Failed to migrate legacy data')
    try:
        await bot.tree.sync()
        print('スラッシュコマンドを同期しました')
    except Exception:
        logger.exception('Failed to sync slash commands')

@bot.tree.command(name='start', description='出勤時に使うコマンド。出勤時間を記録します。')
async def start(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)
        user_id = interaction.user.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            result = await fetch_active_session(conn, guild_id, user_id)

            if result:
                await conn.rollback()
                if result[2] == 1:
                    await send_interaction_message(
                        interaction,
                        f'{interaction.user.mention} さん、休憩中のため出勤できません。まずは /restart コマンドで休憩を終了してください。'
                    )
                else:
                    await send_interaction_message(
                        interaction,
                        f'{interaction.user.mention} さん、既に出勤しています。'
                    )
                return

            start_time = now_jst().strftime('%Y-%m-%d %H:%M:%S')
            await conn.execute('''
                INSERT INTO active_sessions (guild_id, user_id, start_time, is_on_break, total_break_duration)
                VALUES (?, ?, ?, 0, 0)
            ''', (guild_id, user_id, start_time))
            await conn.commit()

        await send_interaction_message(
            interaction,
            f'{interaction.user.mention} さん、{start_time} に出勤しました。'
        )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='end', description='退勤時に使うコマンド。退勤時間を記録し、勤務時間を表示します。')
async def end(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)
        user_id = interaction.user.id

        result = await finish_session(DB_DIR, ACTIVE_DB_PATH, guild_id, user_id)
        if result['status'] == 'not_started':
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さん、まだ出勤していません。/start を使用してください。'
            )
            return
        if result['status'] == 'on_break':
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さん、休憩中のため退勤できません。まずは /restart コマンドで休憩を終了してください。'
            )
            return

        hours, remainder = divmod(result['work_duration'], 3600)
        minutes = remainder // 60
        await send_interaction_message(
            interaction,
            f'{interaction.user.mention} さん、退勤しました。勤務時間は {int(hours)}時間{int(minutes)}分です。'
        )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='break', description='休憩を開始するコマンド。休憩時間を記録します。')
async def break_(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)
        user_id = interaction.user.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            result = await fetch_active_session(conn, guild_id, user_id)

            if not result or result[1] is None:
                await conn.rollback()
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さん、まずは /start で出勤してください。'
                )
            elif result[2] == 1:
                await conn.rollback()
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さんは既に休憩中です。'
                )
            else:
                break_start_time = now_jst().strftime('%Y-%m-%d %H:%M:%S')
                await conn.execute(
                    'UPDATE active_sessions SET is_on_break = 1, break_start_time = ? WHERE user_id = ? AND (guild_id = ? OR guild_id = ?)',
                    (break_start_time, user_id, guild_id, LEGACY_GUILD_ID)
                )
                await conn.execute(
                    'INSERT INTO break_records (guild_id, user_id, break_start) VALUES (?, ?, ?)',
                    (guild_id, user_id, break_start_time)
                )
                await conn.commit()
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さん、{break_start_time} に休憩を開始しました。'
                )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='restart', description='休憩を終了するコマンド。累積休憩時間に休憩時間を追加します。')
async def restart(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)
        user_id = interaction.user.id
        async with aiosqlite.connect(ACTIVE_DB_PATH) as conn:
            await conn.execute('BEGIN IMMEDIATE')
            result = await fetch_active_session(conn, guild_id, user_id)

            if not result or result[2] != 1:
                await conn.rollback()
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さん、休憩中ではありません。/break で休憩を開始してください。'
                )
            else:
                break_end = now_jst()
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
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さん、{break_end.strftime("%H:%M")} に休憩を終了しました。'
                )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='monthly', description='今月の合計勤務時間を表示するコマンドです。')
async def monthly(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)

        user_id = interaction.user.id
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
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さんの今月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。'
            )
        else:
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さん、今月の勤務履歴はありません。'
            )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

@bot.tree.command(name='last_monthly', description='先月の合計勤務時間を表示するコマンドです。')
async def last_monthly(interaction: discord.Interaction):
    try:
        if not await acknowledge_interaction(interaction):
            return

        guild_id = require_guild(interaction)
        if guild_id is None:
            await send_interaction_message(interaction, 'このコマンドはサーバー内でのみ使用できます。')
            return

        await ensure_guild_ready(guild_id)
        await remember_interaction_member(interaction)

        user_id = interaction.user.id
        table_name = f"history_{get_month_key(month_offset=-1)}"
        db_path = get_db_path(month_offset=-1)

        if not os.path.exists(db_path):
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さん、先月の勤務履歴はありません。'
            )
            return

        async with aiosqlite.connect(db_path) as conn:
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ) as cursor:
                table_exists = await cursor.fetchone()

            if not table_exists:
                await send_interaction_message(
                    interaction,
                    f'{interaction.user.mention} さん、先月の勤務履歴はありません。'
                )
                return

            async with conn.execute(f'''
                SELECT SUM(work_duration) FROM {table_name}
                WHERE user_id = ? AND guild_id = ?
            ''', (user_id, guild_id)) as cursor:
                row = await cursor.fetchone()
                total_seconds = row[0]

        if total_seconds:
            hours, remainder = divmod(total_seconds, 3600)
            minutes = remainder // 60
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さんの先月の合計勤務時間は {int(hours)}時間{int(minutes)}分です。'
            )
        else:
            await send_interaction_message(
                interaction,
                f'{interaction.user.mention} さん、先月の勤務履歴はありません。'
            )
    except Exception:
        logger.exception('Command failed')
        await send_interaction_message(interaction, GENERIC_ERROR_MESSAGE)

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
