"""
[AI Agent Summary]
このファイルは、ユーザーごとの好感度（親密度・信頼度）やAIから見た一言所感（印象）を確認するためのスラッシュコマンド（/relationship, /affinity）を提供する Mixin です。
This mixin provides slash commands (/relationship, /affinity) to view user affinity, trust, and AI's personal impression/thought for users.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from lib.date_utils import format_jst_datetime
from lib.text_utils import truncate_text


import discord
from discord import app_commands
from discord.ext import commands

from ...common.config_helpers import cfg
from ..ollama_chat_types import UserRelationship
from .relationships_logic import (
    _copy_relationship,
    _fallback_user_thought,
    _relationship_rank_info,
    _render_relationship_bar,
    _clamp_relationship_value,
)
from .relationships_thought_logic import should_update_user_thought, looks_like_placeholder_thought

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionCommandsBase = OllamaChatProtocol
else:
    _EmotionCommandsBase = object

log = logging.getLogger("ollama_bot.ollama_chat")



class OllamaChatEmotionCommandsMixin(_EmotionCommandsBase):
    """好感度・信頼度および相手への一言所感を確認するスラッシュコマンドを提供するMixin。"""

    async def _send_relationship_embed(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User | discord.Member] = None,
    ) -> None:
        target_user = user or interaction.user
        target_id = int(target_user.id)
        log.info(
            "[TRACE relationship] start interaction_id=%s author=%s target=%s(ID: %s)",
            getattr(interaction, "id", None),
            getattr(interaction.user, "display_name", "?"),
            target_user.display_name,
            target_id,
        )
        try:
            response = getattr(interaction, "response", None)
            is_done_fn = getattr(response, "is_done", None)
            if response is not None and (not callable(is_done_fn) or not is_done_fn()):
                try:
                    await response.defer(ephemeral=False)
                except Exception as defer_err:
                    log.warning("[TRACE relationship] defer skipped/failed: %r", defer_err)

            self._ensure_relationship_state()
            async with self._relationship_lock:
                rel = _copy_relationship(self.user_relationships.get(target_id))

            base_name = str(
                getattr(self.emotion_state, "base_display_name", "") or cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "AI"
            ).strip() or "AI"

            thought = str(rel.thought or "").strip()
            is_fallback = False
            if not thought or looks_like_placeholder_thought(thought):
                is_fallback = True
                thought = _fallback_user_thought(rel, base_name=base_name)
                # メモリ上および永続化ファイルのプレースホルダーをリセット・修復
                if looks_like_placeholder_thought(rel.thought):
                    need_save = False
                    async with self._relationship_lock:
                        live_rel = self.user_relationships.get(target_id)
                        if live_rel is not None and looks_like_placeholder_thought(live_rel.thought):
                            live_rel.thought = thought
                            need_save = True
                    if need_save:
                        await self._save_user_relationships()

            log.info(
                "[関係性コマンド表示] ユーザー: %s(ID: %s) thought=%r (フォールバック定型文=%s)",
                target_user.display_name,
                target_id,
                thought,
                is_fallback,
            )

            # 一言コメントが未生成または更新可能な場合、バックグラウンドでLLM生成をスケジュール
            update_fn = getattr(self, "_update_user_thought_async", None)
            if callable(update_fn) and should_update_user_thought(rel, current_ts=time.time()):
                asyncio.create_task(update_fn(target_id, user_name=target_user.display_name))

            rank_name, rank_desc, color_code = _relationship_rank_info(rel)
            affinity_bar = _render_relationship_bar(rel.affinity)
            trust_bar = _render_relationship_bar(rel.trust)
            affection_bar = _render_relationship_bar(getattr(rel, "affection", 0.0) or 0.0)
            hatred_bar = _render_relationship_bar(getattr(rel, "hatred", 0.0) or 0.0)

            if rel.last_interaction_ts > 0:
                last_seen_str = format_jst_datetime(rel.last_interaction_ts, "%Y/%m/%d %H:%M")
            else:
                last_seen_str = "対話記録なし"

            try:
                from harness.agent.memory_manager.user_identity_resolver import (
                    get_user_aliases_from_store,
                    render_user_facing_text,
                )
                aliases = await get_user_aliases_from_store(
                    target_id,
                    guild=interaction.guild,
                    memory_store=getattr(self, "memory_store", None),
                )
                thought = render_user_facing_text(
                    thought,
                    target_user_id=target_id,
                    current_display_name=target_user.display_name,
                    aliases=aliases,
                )
            except Exception:
                pass

            safe_thought = truncate_text(thought, max_len=900)

            embed = discord.Embed(
                title=f"🎯 {target_user.display_name} との関係性ステータス",
                description=f"{base_name} から見た **{target_user.mention}** に対する好感度・信頼度と所感です。",
                color=color_code,
            )

            # アバターアイコン
            avatar_url = getattr(target_user.display_avatar, "url", None)
            if avatar_url:
                embed.set_thumbnail(url=str(avatar_url))

            embed.add_field(
                name="✨ 関係性ランク",
                value=f"**{rank_name}**\n*{rank_desc}*",
                inline=False,
            )
            embed.add_field(
                name="💖 親密度 (Affinity)",
                value=f"{affinity_bar} ` {rel.affinity:+.2f} `",
                inline=True,
            )
            embed.add_field(
                name="🛡️ 信頼度 (Trust)",
                value=f"{trust_bar} ` {rel.trust:+.2f} `",
                inline=True,
            )
            embed.add_field(
                name="🌸 好意 (Affection)",
                value=f"{affection_bar} ` {getattr(rel, 'affection', 0.0) or 0.0:+.2f} `",
                inline=True,
            )
            embed.add_field(
                name="🔥 憎しみ (Hatred)",
                value=f"{hatred_bar} ` {getattr(rel, 'hatred', 0.0) or 0.0:+.2f} `",
                inline=True,
            )
            embed.add_field(
                name=f"💭 {base_name} からの一言所感",
                value=f">>> **「{safe_thought}」**",
                inline=False,
            )

            embed.set_footer(text=f"最終対話: {last_seen_str}")
            await interaction.followup.send(embed=embed)
            log.info("[TRACE relationship] successfully sent embed for user_id=%s", target_id)
        except Exception as e:
            log.exception("relationship command error: %s", e)
            try:
                await interaction.followup.send(
                    "❌ 関係性ステータスの表示中にエラーが発生しました。",
                    ephemeral=True,
                )
            except Exception:
                pass

    @app_commands.command(
        name="relationship",
        description="AIから見た相手（または自分）への好感度・信頼度と一言所感を表示します。",
    )
    @app_commands.describe(user="確認したい相手のユーザー（未指定の場合は自分）")
    async def show_relationship(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User | discord.Member] = None,
    ) -> None:
        await self._send_relationship_embed(interaction, user=user)

    @app_commands.command(
        name="set_lover",
        description="ユーザーの恋人・最愛のパートナー関係フラグを設定・更新します（管理者用）。",
    )
    @app_commands.describe(
        user="設定対象のユーザー",
        is_lover="恋人関係フラグ（Trueで恋人設定、Falseで解除）",
    )
    async def slash_set_lover(
        self,
        interaction: discord.Interaction,
        user: discord.User | discord.Member,
        is_lover: bool = True,
    ) -> None:
        try:
            response = getattr(interaction, "response", None)
            is_done_fn = getattr(response, "is_done", None)
            if response is not None and (not callable(is_done_fn) or not is_done_fn()):
                await response.defer(ephemeral=True)
        except Exception as defer_err:
            log.warning("[TRACE set_lover] defer failed: %r", defer_err)

        # 管理者チェック: is_bot_admin_user, サーバー管理者権限, またはマーモン本人
        operator_id = interaction.user.id
        is_admin = False
        admin_check = getattr(self, "is_bot_admin_user", None)
        if callable(admin_check) and admin_check(operator_id):
            is_admin = True
        elif getattr(getattr(interaction.user, "guild_permissions", None), "administrator", False):
            is_admin = True
        elif operator_id == 534073690535624734:
            is_admin = True

        if not is_admin:
            try:
                await interaction.followup.send(
                    "❌ このコマンドを実行する管理者権限がありません。",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        target_id = int(user.id)
        self._ensure_relationship_state()
        async with self._relationship_lock:
            rel = self.user_relationships.get(target_id)
            if rel is None:
                rel = UserRelationship(
                    affinity=5.0 if is_lover else 0.0,
                    trust=5.0 if is_lover else 0.0,
                    affection=5.0 if is_lover else 0.0,
                    hatred=0.0,
                    thought="お互いに想いを通わせ合った、最愛で特別な恋人！" if is_lover else "まだ知り合って間もない、普通のお知り合い。",
                    is_lover=is_lover,
                )
                self.user_relationships[target_id] = rel
            else:
                rel.is_lover = is_lover
                if is_lover and (not rel.thought or looks_like_placeholder_thought(rel.thought)):
                    rel.thought = "お互いに想いを通わせ合った、最愛で特別な恋人！"

        await self._save_user_relationships()
        log.info(
            "[set_lover] operator=%s(ID: %s) target=%s(ID: %s) is_lover=%s",
            getattr(interaction.user, "display_name", "?"),
            operator_id,
            user.display_name,
            target_id,
            is_lover,
        )
        try:
            status_text = "💍 恋人・最愛のパートナーに設定しました。" if is_lover else "恋人フラグを解除しました。"
            await interaction.followup.send(
                f"✅ {user.mention} のステータスを {status_text}",
                ephemeral=True,
            )
        except Exception as e:
            log.warning("[set_lover] followup.send failed: %r", e)

