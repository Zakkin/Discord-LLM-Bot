"""ティア表パーティゲームのDiscord Cogモジュール。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from lib.config_utils import cfg, cfg_int, cfg_int_set
from .models import GamePhase
from .renderer import render_lobby_embed
from .session import TierGameSession
from .topics import GLOBAL_TOPIC_POOL
from .views import TierLobbyView

log = logging.getLogger("tier_game.cog")


class TierGameCog(commands.Cog):
    """ティア表パーティゲームを統括するCog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.active_sessions: dict[int, TierGameSession] = {}
        self._session_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        """Cog読み込み時にお題プールをバックグラウンドで先行充填する。"""
        GLOBAL_TOPIC_POOL.trigger_fill(bot=self.bot)

    @property
    def target_channel_id(self) -> int:
        """ゲーム専用チャンネルID（0の場合は全チャンネル許可）。"""
        return cfg_int("TIER_GAME_CHANNEL_ID", 0)

    @property
    def admin_user_ids(self) -> set[int]:
        """管理者ユーザーIDセット。"""
        return cfg_int_set("BOT_ADMIN_USER_IDS")

    async def _cleanup_session(self, channel_id: int) -> None:
        """セッション終了時のクリーンアップ。"""
        async with self._session_lock:
            self.active_sessions.pop(channel_id, None)
            log.info("TierGameSession removed for channel %s", channel_id)

    async def _resolve_game_channel(
        self,
        current_channel: Any,
    ) -> tuple[Any, bool]:
        """設定された指定チャンネルを解決し、現在チャンネルと異なるか（誘導が必要か）を返す。"""
        target_cid = self.target_channel_id
        if target_cid <= 0:
            return current_channel, False

        curr_id = getattr(current_channel, "id", 0)
        if curr_id == target_cid:
            return current_channel, False

        # 指定チャンネルのオブジェクトを取得
        target_channel = self.bot.get_channel(target_cid)
        if target_channel is None:
            try:
                target_channel = await self.bot.fetch_channel(target_cid)
            except Exception as e:
                log.warning("Failed to fetch target tier game channel %s: %r", target_cid, e)

        if target_channel is not None and callable(getattr(target_channel, "send", None)):
            return target_channel, True

        log.warning("Target tier game channel %s not found or not messageable; using current channel", target_cid)
        return current_channel, False

    # -------------------------------------------------------------
    # スラッシュコマンド: /tier_game
    # -------------------------------------------------------------
    @app_commands.command(
        name="tier_game",
        description="格ゲー等でおなじみの『ティア表』を使った心理パーティゲームを開始します！",
    )
    async def slash_tier_game(self, interaction: discord.Interaction) -> None:
        user = interaction.user
        current_chan = interaction.channel
        if current_chan is None or not callable(getattr(current_chan, "send", None)):
            await interaction.response.send_message("❌ このチャンネルでは利用できません。", ephemeral=True)
            return

        game_chan, needs_redirect = await self._resolve_game_channel(current_chan)
        game_cid = game_chan.id

        async with self._session_lock:
            if game_cid in self.active_sessions:
                existing = self.active_sessions[game_cid]
                if existing.phase != GamePhase.GAME_OVER:
                    loc_str = f"<#{game_cid}>" if needs_redirect else "このチャンネル"
                    await interaction.response.send_message(
                        f"⚠️ {loc_str} では既にティア表パーティゲームが進行中です！",
                        ephemeral=True,
                    )
                    return

            # 新セッションの作成
            session = TierGameSession(
                channel=game_chan,
                creator=user,
                on_cleanup=self._cleanup_session,
                admin_user_ids=self.admin_user_ids,
                bot=self.bot,
            )
            self.active_sessions[game_cid] = session

        # 誘導メッセージの送信
        if needs_redirect:
            await interaction.response.send_message(
                f"🎮 **ティア表パーティゲームを開始します！**\n"
                f"ゲーム会場は <#{game_cid}> です。参加する方はそちらへ移動してください！"
            )
        else:
            await interaction.response.send_message(
                "🎮 **ティア表パーティゲームを開始します！参加者を募集しています！**"
            )

        # 指定チャンネル側でロビーメッセージを投稿
        embed = render_lobby_embed(user.display_name, list(session.players.values()), False)
        view = TierLobbyView(session)
        msg = await game_chan.send(
            content=f"🎮 **ティア表パーティゲーム - 参加者募集中！**\n（募集者: **{user.mention}**）",
            embed=embed,
            view=view,
        )
        session.main_message = msg

        # ロビー募集中にバックグラウンドでお題プールを先行生成（即時開始用）
        guild = getattr(game_chan, "guild", None)
        gid = getattr(guild, "id", None) if guild is not None else None
        GLOBAL_TOPIC_POOL.trigger_fill(bot=self.bot, guild_id=gid, channel_id=game_cid)

    # -------------------------------------------------------------
    # プレフィックスコマンド: !tier_game
    # -------------------------------------------------------------
    @commands.command(name="tier_game", aliases=["tier"])
    async def cmd_tier_game(self, ctx: commands.Context) -> None:
        try:
            from ollama_bot.common.bot_identity import is_target_of_command
            msg = getattr(ctx, "message", None)
            if msg is not None and not is_target_of_command(self.bot, msg):
                return
        except Exception:
            pass

        user = ctx.author
        current_chan = ctx.channel
        if current_chan is None or not callable(getattr(current_chan, "send", None)):
            return

        game_chan, needs_redirect = await self._resolve_game_channel(current_chan)
        game_cid = game_chan.id

        async with self._session_lock:
            if game_cid in self.active_sessions:
                existing = self.active_sessions[game_cid]
                if existing.phase != GamePhase.GAME_OVER:
                    loc_str = f"<#{game_cid}>" if needs_redirect else "このチャンネル"
                    await ctx.send(f"⚠️ {loc_str} では既にティア表パーティゲームが進行中です！")
                    return

            session = TierGameSession(
                channel=game_chan,
                creator=user,
                on_cleanup=self._cleanup_session,
                admin_user_ids=self.admin_user_ids,
                bot=self.bot,
            )
            self.active_sessions[game_cid] = session

        if needs_redirect:
            await ctx.send(
                f"🎮 **ティア表パーティゲームを開始します！**\n"
                f"ゲーム会場は <#{game_cid}> です。参加する方はそちらへ移動してください！"
            )
        else:
            await ctx.send("🎮 **ティア表パーティゲームを開始します！参加者を募集しています！**")

        embed = render_lobby_embed(user.display_name, list(session.players.values()), False)
        view = TierLobbyView(session)
        msg = await game_chan.send(
            content=f"🎮 **ティア表パーティゲーム - 参加者募集中！**\n（募集者: **{user.mention}**）",
            embed=embed,
            view=view,
        )
        session.main_message = msg

        # ロビー募集中にバックグラウンドでお題プールを先行生成（即時開始用）
        guild = getattr(game_chan, "guild", None)
        gid = getattr(guild, "id", None) if guild is not None else None
        GLOBAL_TOPIC_POOL.trigger_fill(bot=self.bot, guild_id=gid, channel_id=game_cid)

    # -------------------------------------------------------------
    # スラッシュコマンド: /tier_stop
    # -------------------------------------------------------------
    @app_commands.command(
        name="tier_stop",
        description="進行中のティア表パーティゲームを強制終了します（ホストまたは管理者限定）。",
    )
    async def slash_tier_stop(self, interaction: discord.Interaction) -> None:
        current_chan = interaction.channel
        if current_chan is None or not callable(getattr(current_chan, "send", None)):
            await interaction.response.send_message("❌ このチャンネルでは利用できません。", ephemeral=True)
            return

        game_chan, _ = await self._resolve_game_channel(current_chan)
        game_cid = game_chan.id

        session = self.active_sessions.get(game_cid)
        if session is None or session.phase == GamePhase.GAME_OVER:
            await interaction.response.send_message("現在進行中のティア表ゲームはありません。", ephemeral=True)
            return

        if not session.is_host_or_admin(interaction.user.id):
            await interaction.response.send_message("❌ ゲームを終了できるのはホストまたは管理者のみです。", ephemeral=True)
            return

        await session.cancel_game()
        await interaction.response.send_message("🛑 ティア表パーティゲームを強制終了しました。")

    # -------------------------------------------------------------
    # プレフィックスコマンド: !tier_stop
    # -------------------------------------------------------------
    @commands.command(name="tier_stop")
    async def cmd_tier_stop(self, ctx: commands.Context) -> None:
        try:
            from ollama_bot.common.bot_identity import is_target_of_command
            msg = getattr(ctx, "message", None)
            if msg is not None and not is_target_of_command(self.bot, msg):
                return
        except Exception:
            pass

        current_chan = ctx.channel
        if current_chan is None or not callable(getattr(current_chan, "send", None)):
            return

        game_chan, _ = await self._resolve_game_channel(current_chan)
        game_cid = game_chan.id

        session = self.active_sessions.get(game_cid)
        if session is None or session.phase == GamePhase.GAME_OVER:
            await ctx.send("現在進行中のティア表ゲームはありません。")
            return

        if not session.is_host_or_admin(ctx.author.id):
            await ctx.send("❌ ゲームを終了できるのはホストまたは管理者のみです。")
            return

        await session.cancel_game()
        await ctx.send("🛑 ティア表パーティゲームを強制終了しました。")


async def setup_tier_game(bot: commands.Bot) -> None:
    """TierGameCog をBotに登録するヘルパー関数。"""
    await bot.add_cog(TierGameCog(bot))
