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
        await interaction.response.defer(ephemeral=False)
        try:
            target_user = user or interaction.user
            target_id = int(target_user.id)

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
                value=f">>> **「{thought}」**",
                inline=False,
            )

            embed.set_footer(text=f"最終対話: {last_seen_str}")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            log.exception("relationship command error: %s", e)
            await interaction.followup.send(
                "❌ 関係性ステータスの表示中にエラーが発生しました。",
                ephemeral=True,
            )

    @app_commands.command(
        name="relationship",
        description="AIから見た相手（または自分）への好感度・信頼度と一言所感を表示します。",
    )
    @app_commands.describe(user="確認したい相手のユーザー（未指定の場合は自分）")
    async def show_relationship(
        self,
        interaction: discord.Interaction,
        user: Optional[discord.User] = None,
    ) -> None:
        await self._send_relationship_embed(interaction, user=user)

