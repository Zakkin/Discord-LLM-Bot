"""Botの稼働状態（起動・停止・ステータス確認）および管理者権限制限を担当するMixin。"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, TYPE_CHECKING

import discord
from discord import app_commands

from ..common.config_helpers import cfg, cfg_int_set
from ..common.discord_helpers import author_id, content
from ..common.emotion_helpers import emotion_scoring_enabled

if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _AdminBase = OllamaChatProtocol
else:
    _AdminBase = object

log = logging.getLogger("ollama_bot.ollama_chat_.admin")


def is_bot_admin_user(user_id: int | None) -> bool:
    """指定されたユーザーIDが管理者であるかを判定する。
    
    BOT_ADMIN_USER_IDS または ADMIN_USER_IDS に含まれているかを検証する。
    未設定（空）の場合は安全のため常に False を返す。
    """
    if user_id is None:
        return False
    admin_ids = cfg_int_set("BOT_ADMIN_USER_IDS") or cfg_int_set("ADMIN_USER_IDS")
    return user_id in admin_ids


class OllamaChatAdminMixin(_AdminBase):
    def is_bot_admin_user(self, user_id: int | None) -> bool:
        """指定されたユーザーIDが管理者であるかを判定する。"""
        return is_bot_admin_user(user_id)

    async def set_bot_active(self, active: bool, operator_name: str = "") -> tuple[bool, str]:
        """Botの稼働状態を切り替え、Presenceや実行中タスクを更新する。"""
        current = getattr(self, "_bot_active", True)
        if current == active:
            if active:
                return False, "ℹ️ Botはすでに起動・稼働中です。"
            return False, "ℹ️ Botはすでに停止（スタンバイ）中です。"

        self._bot_active = active

        if not active:
            log.warning("Bot deactivated (stopped) by operator=%s", operator_name)
            # 停止中のPresence設定
            stopped_text = str(cfg("BOT_STOPPED_ACTIVITY_TEXT", "停止中（スタンバイ）") or "停止中（スタンバイ）")
            try:
                status_dnd = getattr(discord.Status, "dnd", "dnd")
                activity = discord.CustomActivity(name=stopped_text)
                await self.bot.change_presence(status=status_dnd, activity=activity)
                if hasattr(self, "emotion_state") and hasattr(self, "_emotion_lock"):
                    async with self._emotion_lock:
                        self.emotion_state.last_applied_activity = stopped_text
            except Exception as e:
                log.warning("Failed to update presence on bot stop: %r", e)

            # 実行中の背景LLMタスクがあればキャンセル
            bg_task = getattr(self, "_current_background_llm_task", None)
            if bg_task is not None and not bg_task.done():
                bg_task.cancel()

            # 歌唱タスクがあれば中断
            if hasattr(self, "singing_tasks"):
                for task in list(self.singing_tasks.values()):
                    if not task.done():
                        task.cancel()
                self.singing_tasks.clear()

            msg = "🛑 **Botの応答機能を停止しました。**\nAIチャット返信および自発発言をスタンバイ状態に移行しました。"
            return True, msg

        log.info("Bot activated (started) by operator=%s", operator_name)
        # 起動時のPresence復帰（感情連動Presenceを強制即時反映）
        try:
            if hasattr(self, "_sync_emotion_presence") and emotion_scoring_enabled():
                await self._sync_emotion_presence(force=True)
            else:
                status_online = getattr(discord.Status, "online", "online")
                await self.bot.change_presence(status=status_online, activity=None)
        except Exception as e:
            log.warning("Failed to restore presence on bot start: %r", e)

        msg = "▶️ **Botの応答機能を起動・再開しました。**\nメッセージ応答および自発発言を再開します。"
        return True, msg

    @app_commands.command(
        name="bot",
        description="Botの稼働状態（起動・停止・確認）を管理します。",
    )
    @app_commands.describe(action="実行する操作（start: 起動, stop: 停止, status: 状態確認）")
    @app_commands.choices(action=[
        app_commands.Choice(name="start (起動・再開)", value="start"),
        app_commands.Choice(name="stop (一時停止)", value="stop"),
        app_commands.Choice(name="status (状態確認)", value="status"),
    ])
    async def slash_bot(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
    ) -> None:
        cmd_action = action.value
        user_id = getattr(interaction.user, "id", None)
        operator_name = getattr(interaction.user, "display_name", str(interaction.user))

        if cmd_action == "start":
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /bot start attempt from user_id=%s", user_id)
                await interaction.response.send_message(
                    "❌ このコマンドを実行する権限がありません。（管理者専用）",
                    ephemeral=True,
                )
                return
            _, msg = await self.set_bot_active(True, operator_name=operator_name)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        if cmd_action == "stop":
            if not self.is_bot_admin_user(user_id):
                log.warning("Unauthorized /bot stop attempt from user_id=%s", user_id)
                await interaction.response.send_message(
                    "❌ このコマンドを実行する権限がありません。（管理者専用）",
                    ephemeral=True,
                )
                return
            _, msg = await self.set_bot_active(False, operator_name=operator_name)
            await interaction.response.send_message(msg, ephemeral=True)
            return

        # status
        active = getattr(self, "is_active", True)
        if active:
            msg = "🟢 **Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
        else:
            msg = "🔴 **Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_start",
        description="【管理者専用】Botの応答機能を起動・再開します",
    )
    async def slash_bot_start(self, interaction: discord.Interaction) -> None:
        user_id = getattr(interaction.user, "id", None)
        if not self.is_bot_admin_user(user_id):
            log.warning("Unauthorized /bot_start attempt from user_id=%s", user_id)
            await interaction.response.send_message(
                "❌ このコマンドを実行する権限がありません。（管理者専用）",
                ephemeral=True,
            )
            return

        operator_name = getattr(interaction.user, "display_name", str(interaction.user))
        _, msg = await self.set_bot_active(True, operator_name=operator_name)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_stop",
        description="【管理者専用】Botの応答機能を一時停止します",
    )
    async def slash_bot_stop(self, interaction: discord.Interaction) -> None:
        user_id = getattr(interaction.user, "id", None)
        if not self.is_bot_admin_user(user_id):
            log.warning("Unauthorized /bot_stop attempt from user_id=%s", user_id)
            await interaction.response.send_message(
                "❌ このコマンドを実行する権限がありません。（管理者専用）",
                ephemeral=True,
            )
            return

        operator_name = getattr(interaction.user, "display_name", str(interaction.user))
        _, msg = await self.set_bot_active(False, operator_name=operator_name)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(
        name="bot_status",
        description="Botの現在の稼働状態（稼働中／停止中）を確認します",
    )
    async def slash_bot_status(self, interaction: discord.Interaction) -> None:
        active = getattr(self, "is_active", True)
        if active:
            msg = "🟢 **Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
        else:
            msg = "🔴 **Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
        await interaction.response.send_message(msg, ephemeral=True)

    async def _handle_admin_text_command(self, message: discord.Message) -> bool:
        """`!bot` 形式のテキストコマンドを処理する。
        
        戻り値: コマンドとして処理された場合は True、対象外の場合は False。
        """
        raw_text = content(message).strip()
        parts = raw_text.split()
        if not parts or parts[0].lower() != "!bot":
            return False

        subcommand = parts[1].lower() if len(parts) > 1 else "status"
        u_id = author_id(message)
        author_name = getattr(message.author, "display_name", str(message.author))

        if subcommand == "start":
            if not self.is_bot_admin_user(u_id):
                log.warning("Unauthorized !bot start attempt from author_id=%s", u_id)
                await message.reply("❌ このコマンドを実行する権限がありません。（管理者専用）", mention_author=False)
                return True
            _, msg = await self.set_bot_active(True, operator_name=author_name)
            await message.reply(msg, mention_author=False)
            return True

        if subcommand == "stop":
            if not self.is_bot_admin_user(u_id):
                log.warning("Unauthorized !bot stop attempt from author_id=%s", u_id)
                await message.reply("❌ このコマンドを実行する権限がありません。（管理者専用）", mention_author=False)
                return True
            _, msg = await self.set_bot_active(False, operator_name=author_name)
            await message.reply(msg, mention_author=False)
            return True

        if subcommand == "status":
            active = getattr(self, "is_active", True)
            if active:
                msg = "🟢 **Botは現在稼働中です。**\n通常の会話、ゲーム、自発発言等を受け付けています。"
            else:
                msg = "🔴 **Botは現在停止中です（スタンバイ状態）。**\n管理者が再開するまでAI返信や自発発言は行われません。"
            await message.reply(msg, mention_author=False)
            return True

        # 未知のサブコマンド
        await message.reply(
            "ℹ️ 使用法: `!bot start`（起動・再開）, `!bot stop`（停止）, `!bot status`（状態確認）",
            mention_author=False,
        )
        return True
