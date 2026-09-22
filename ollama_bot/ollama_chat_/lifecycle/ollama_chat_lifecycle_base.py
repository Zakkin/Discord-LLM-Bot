"""Cogのライフサイクル（load/unload）、コンテキストメニューの登録、キャッシュ注入などの基本処理を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import re
from typing import Optional, Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ...config_loader import config
from ..ollama_chat_helpers import *
from lib.discord_utils import inject_action_history

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _OllamaChatLifecycleBase = OllamaChatProtocol
else:
    _OllamaChatLifecycleBase = object


class OllamaChatLifecycleBaseMixin(_OllamaChatLifecycleBase):
        def _context_menus(self) -> tuple[app_commands.ContextMenu, ...]:
            return (
                self._fact_check_menu,
                self._summarize_menu,
                self._simplify_menu,
                self._what_is_this_menu,
            )

        async def cog_unload(self) -> None:
            for menu in self._context_menus():
                try:
                    self.bot.tree.remove_command(menu.name, type=menu.type)
                except Exception:
                    pass
            if self.topic_task:
                self.topic_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.topic_task
            if self.agent_task:
                self.agent_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.agent_task
            if self._reflection_task:
                self._reflection_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reflection_task
            if self._emotion_decay_task:
                self._emotion_decay_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._emotion_decay_task
            if self._habit_decay_task:
                self._habit_decay_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._habit_decay_task
            await close_shared_http_session()

        async def cog_load(self) -> None:
            for menu in self._context_menus():
                try:
                    self.bot.tree.remove_command(menu.name, type=menu.type)
                except Exception:
                    pass
                self.bot.tree.add_command(menu)

        def _inject_context_cache(
            self,
            interaction: discord.Interaction,
            action_name: str,
            result_text: str,
        ) -> None:
            cid = getattr(interaction, "channel_id", None)
            if cid is None:
                return
            user_name = self._requester_label_from_interaction(interaction)
            guild_me = getattr(getattr(interaction, "guild", None), "me", None)
            bot_name = getattr(guild_me, "display_name", None) or str(cfg("OLLAMA_BOT_DISPLAY_NAME", "AI"))
            inject_action_history(
                self.channel_context_cache[cid],
                user_label=user_name,
                bot_name=bot_name,
                action_name=action_name,
                result_text=result_text,
            )

        def _inject_message_context_cache(
            self,
            message: discord.Message,
            action_name: str,
            result_text: str,
        ) -> None:
            cid = getattr(message.channel, "id", None)
            if cid is None:
                return
            user_name = self._requester_label_from_message(message)
            guild_me = getattr(getattr(message, "guild", None), "me", None)
            bot_name = getattr(guild_me, "display_name", None) or str(cfg("OLLAMA_BOT_DISPLAY_NAME", "AI"))
            inject_action_history(
                self.channel_context_cache[cid],
                user_label=user_name,
                bot_name=bot_name,
                action_name=action_name,
                result_text=result_text,
            )

        def _build_target_block(
            self,
            target_text: str,
            fact_context,
            *,
            header: str,
        ) -> str:
            target_block = f"{header}\n{target_text}\n\n"
            if getattr(fact_context, "link_context_block", ""):
                target_block += f"{fact_context.link_context_block}\n\n"
            return target_block

        async def _reply_with_embed_or_fallback(
            self,
            message: discord.Message,
            *,
            embed: discord.Embed,
            fallback_heading: str,
            fallback_text: str,
        ) -> None:
            try:
                await message.reply(embed=embed, mention_author=False)
            except discord.Forbidden:
                await message.reply(
                    f"{fallback_heading}\n{fallback_text}",
                    mention_author=False,
                )

        def _build_dynamic_system_prompt(self, base_system_prompt: Optional[str]) -> str:
            extra = self._build_time_rhythm_guidance().strip()
            base = str(base_system_prompt or "").strip()
            if base and extra:
                return f"{base}\n\n[時間帯の補足]\n{extra}"
            return extra or base

