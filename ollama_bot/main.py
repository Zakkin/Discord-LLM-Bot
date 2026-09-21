import logging
import os

try:
    import asyncio
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass
import gzip
import shutil
from logging.handlers import TimedRotatingFileHandler
import discord
from discord.ext import commands

from .config_loader import config
from .common.config_helpers import cfg_managed_channel_ids, cfg_primary_channel_id
from lib.config_utils import cfg_int_set


def _command_sync_scope() -> str:
    return str(getattr(config, "APP_COMMAND_SYNC_SCOPE", "guild") or "guild").strip().lower()


from .ollama_chat import setup_ollama_chat
from .common.ollama_helpers import close_ollama_session

def _make_rotating_file_handler(log_path: str, archive_dir: str) -> TimedRotatingFileHandler:
    """daily rotation + gzip圧縮 → archive/ に保存するハンドラを生成する。"""
    handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)7s] %(name)s: %(message)s"))

    def _namer(default_name: str) -> str:
        return default_name

    def _rotator(source: str, dest: str) -> None:
        base_name = os.path.basename(dest)
        archive_path = os.path.join(archive_dir, base_name + ".gz")
        try:
            with open(source, "rb") as f_in, gzip.open(archive_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(source)
        except Exception:
            pass

    handler.namer = _namer
    handler.rotator = _rotator
    return handler


def _setup_logging() -> None:
    log_dir = "logs"
    archive_dir = os.path.join(log_dir, "archive")
    os.makedirs(archive_dir, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(config.LOG_LEVEL)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # コンソールハンドラ（全ログ）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)7s] %(name)s: %(message)s"))
    root_logger.addHandler(console_handler)

    # bot.log（全ログ daily rotation + gzip）
    root_logger.addHandler(_make_rotating_file_handler(
        os.path.join(log_dir, "bot.log"), archive_dir
    ))

    # llm.log（LLMリクエスト/レスポンス専用 daily rotation + gzip）
    # ollama_bot.common.ollama_helpers のログのみを分離して記録する
    llm_handler = _make_rotating_file_handler(
        os.path.join(log_dir, "llm.log"), archive_dir
    )
    llm_handler.setLevel(logging.INFO)
    llm_logger = logging.getLogger("ollama_bot.common.ollama_helpers")
    # propagate=True のまま root に流しつつ、専用ファイルにも記録
    llm_logger.addHandler(llm_handler)


_setup_logging()
log = logging.getLogger("ollama_bot.main")


class OllamaBot(commands.Bot):
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        user_id = getattr(interaction.user, "id", None)
        if user_id is not None:
            ignored_user_ids = cfg_int_set("IGNORE_USER_IDS")
            if user_id in ignored_user_ids:
                log.info(
                    "TRACE on_interaction: ignore user drop interaction_id=%s user_id=%s type=%s",
                    getattr(interaction, "id", None),
                    user_id,
                    getattr(interaction, "type", None),
                )
                # オートコンプリートはメッセージ送信不可のためスキップ
                is_autocomplete = (
                    hasattr(discord, "InteractionType")
                    and getattr(interaction, "type", None) == discord.InteractionType.autocomplete
                )
                if not is_autocomplete and not interaction.response.is_done():
                    try:
                        await interaction.response.send_message(
                            "❌ この機能はご利用いただけません。",
                            ephemeral=True,
                        )
                    except Exception as e:
                        log.debug("Failed to send ignore ephemeral reply to user %s: %r", user_id, e)
                return

        # discord.py の commands.Bot / Client には基底 on_interaction は定義されていないため
        # super() 呼び出しは行わない（呼ぶと AttributeError になる）
        pass

    async def close(self) -> None:
        try:
            cog = self.get_cog("OllamaChatCog")
            if cog is not None and hasattr(cog, "memory_store"):
                await cog.memory_store.close()
        finally:
            try:
                await close_ollama_session()
            finally:
                await super().close()


def build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.messages = True
    intents.message_content = True
    intents.members = True
    intents.reactions = True

    bot = OllamaBot(command_prefix="!", intents=intents)

    async def _tree_interaction_check(interaction: discord.Interaction) -> bool:
        user_id = getattr(interaction.user, "id", None)
        if user_id is not None and user_id in cfg_int_set("IGNORE_USER_IDS"):
            log.info("TRACE tree_interaction_check: blocked ignored user %s", user_id)
            is_autocomplete = (
                hasattr(discord, "InteractionType")
                and getattr(interaction, "type", None) == discord.InteractionType.autocomplete
            )
            if not is_autocomplete and not interaction.response.is_done():
                try:
                    await interaction.response.send_message(
                        "❌ この機能はご利用いただけません。",
                        ephemeral=True,
                    )
                except Exception as e:
                    log.debug("Failed to send ignore ephemeral in tree_interaction_check: %r", e)
            return False
        return True

    bot.tree.interaction_check = _tree_interaction_check

    @bot.event
    async def setup_hook() -> None:
        await setup_ollama_chat(bot)

        # 1. グローバルコマンド（AI系: 20doors, umigame, ファクトチェック等）を同期
        try:
            synced_global = await bot.tree.sync()
            log.info(
                "Synced %d global app commands: %s",
                len(synced_global),
                [getattr(cmd, "name", "?") for cmd in synced_global],
            )
        except Exception as e:
            log.exception("Global command sync failed: %s", e)

        # 2. ギルド専用コマンド（旧管理系コマンドのクリーンアップ含む）を同期
        try:
            guild_obj = discord.Object(id=config.GUILD_ID)
            synced_guild = await bot.tree.sync(guild=guild_obj)
            log.info(
                "Synced %d guild app commands to guild %s: %s",
                len(synced_guild),
                config.GUILD_ID,
                [getattr(cmd, "name", "?") for cmd in synced_guild],
            )
        except Exception as e:
            log.exception("Guild command sync failed for guild %s: %s", config.GUILD_ID, e)

    log.debug("extra_events after setup: %s", list(bot.extra_events.keys()))

    @bot.event
    async def on_ready() -> None:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
        log.info("Bot sees guilds: %s", [(g.id, g.name) for g in bot.guilds])
        log.info("Config file: %s", getattr(config, "__file__", "unknown"))
        log.info("OLLAMA_CHANNEL_ID: %s", cfg_primary_channel_id(0))
        log.info("OTHER_CHANNEL_IDS: %s", getattr(config, "OTHER_CHANNEL_IDS", None))
        log.info(
            "OTHER_CHANNEL_RANDOM_RANGE: %s-%s (ALWAYS=%s)",
            getattr(config, "OTHER_CHANNEL_RANDOM_MIN", None),
            getattr(config, "OTHER_CHANNEL_RANDOM_MAX", None),
            getattr(config, "OTHER_CHANNEL_ALWAYS_RESPOND", None),
        )
        log.info("OLLAMA_ENABLE_THINK_CLASSIFIER: %r", getattr(config, "OLLAMA_ENABLE_THINK_CLASSIFIER", None))
        log.info("OLLAMA_MODEL: %r", getattr(config, "OLLAMA_MODEL", None))
        try:
            guild_obj = discord.Object(id=config.GUILD_ID)
            cmds = bot.tree.get_commands(guild=guild_obj)
            global_cmds = bot.tree.get_commands()
            log.info(
                "Registered guild app commands: %s",
                [getattr(cmd, "name", "?") for cmd in cmds],
            )
            log.info(
                "Registered global app commands: %s",
                [getattr(cmd, "name", "?") for cmd in global_cmds],
            )
        except Exception as e:
            log.exception("command debug dump failed: %s", e)

        try:
            guild = bot.get_guild(config.GUILD_ID)
            log.info("Resolved guild: %r", guild)

            me = guild.me if guild else None
            log.info("Guild me: %r", me)

            channel_ids = sorted(cfg_managed_channel_ids())
            for cid in channel_ids:
                try:
                    ch = bot.get_channel(cid)
                    log.info("bot.get_channel(%s) -> %r", cid, ch)

                    if ch is None:
                        ch = await bot.fetch_channel(cid)
                        log.info("bot.fetch_channel(%s) -> %r", cid, ch)

                    perms = ch.permissions_for(me) if me and hasattr(ch, "permissions_for") else None
                    log.info(
                        "channel=%s name=%s perms view=%s history=%s send=%s",
                        cid,
                        getattr(ch, "name", "?"),
                        getattr(perms, "view_channel", None),
                        getattr(perms, "read_message_history", None),
                        getattr(perms, "send_messages", None),
                    )
                except Exception as e:
                    log.exception("channel check failed cid=%s err=%s", cid, e)
        except Exception as e:
            log.exception("on_ready debug block failed: %s", e)

    return bot

import sys
import fcntl

_lock_fd = None

def main() -> None:
    if not config.TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN が未設定です。環境変数 DISCORD_TOKEN を設定してください。"
        )

    global _lock_fd
    config_name = os.environ.get("OLLAMA_BOT_CONFIG", "default")
    lock_file = f"/tmp/discord_ai_bot_{config_name}.lock"
    
    _lock_fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"CRITICAL: すでに起動しています (Lock file {lock_file} is locked). 二重起動を防止するため終了します。", file=sys.stderr)
        os.close(_lock_fd)
        sys.exit(1)
        
    os.ftruncate(_lock_fd, 0)
    os.write(_lock_fd, str(os.getpid()).encode("utf-8"))

    import time
    import aiohttp

    retry_delay = 5.0
    max_delay = 60.0

    while True:
        try:
            bot = build_bot()
            bot.run(config.TOKEN, log_handler=None)
            break
        except (
            aiohttp.ClientError,
            discord.HTTPException,
            discord.GatewayNotFound,
            discord.ConnectionClosed,
            AttributeError,
        ) as e:
            log.warning(
                "Discord Gateway への接続中に一時的エラーが発生しました (%r)。%.1f 秒後に再試行します...",
                e, retry_delay,
            )
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, max_delay)
        except KeyboardInterrupt:
            log.info("キーボード割り込みによりBotを終了します。")
            break


if __name__ == "__main__":
    main()
    

    
