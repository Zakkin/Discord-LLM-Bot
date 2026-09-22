"""
[AI Agent Summary]
このファイルは、Discord上のステータス（Nickname, Bio, Activity）やリアクションの反映処理を担当する Mixin です。
This mixin handles updating Discord presence (Nickname, Bio, Activity) and adding scored reactions to messages.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, TYPE_CHECKING

import discord

from lib.discord_utils import strip_discord_mentions
from ...common.config_helpers import cfg, cfg_bool, cfg_float, cfg_int
from ...common.discord_helpers import update_context_message_reaction
from ...common.emotion_helpers import (
    EMOTION_LABEL_TO_FACE,
    build_emotion_bio_text,
    build_emotion_status_text,
    build_nickname_with_face,
    emotion_bio_enabled,
    emotion_scoring_enabled,
)
from ...common.ollama_helpers import truncate_text
from ..ollama_chat_types import MessageRuntime

log = logging.getLogger("ollama_bot.ollama_chat")


_FRIENDLY_OR_GREETING_PATTERN = re.compile(
    r"(?:"
    r"初めまして|はじめまして|"
    r"こんにちは|こんばん[はわ]|おはよう[ございま]*[すした]*|"
    r"よろしく[お願ねが]*[い致しま]*[すした]*|"
    r"仲良[くし]*[ましょ]*[うね]*|"
    r"いらっしゃい|ようこそ|"
    r"ありがとう[ございま]*[すした]*|感謝|"
    r"(?:どうぞ|今後とも)[、\s]*よろしく"
    r")"
)


def _is_friendly_or_greeting_message(text: str) -> bool:
    """メッセージが友好的な挨拶や自己紹介、感謝等を含んでいるかを判定する。"""
    cleaned = strip_discord_mentions(text or "").strip()
    return bool(_FRIENDLY_OR_GREETING_PATTERN.search(cleaned))



if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionPresenceBase = OllamaChatProtocol
else:
    _EmotionPresenceBase = object

from .models_logic import _normalize_scored_reaction


class OllamaChatEmotionPresenceMixin(_EmotionPresenceBase):
    def _resolve_scored_reaction_emoji(self, message: discord.Message, reaction: str) -> object | str | None:
        emoji_value = {
            "GOOD": cfg("GOOD_EMOJI", "👍"),
            "BAD": cfg("BAD_EMOJI", "👎"),
        }.get(_normalize_scored_reaction(reaction))
        if emoji_value is None:
            return None
        raw = str(emoji_value).strip()
        if not raw:
            return None
        if raw.isdigit():
            emoji_id = int(raw)
            guild = getattr(message, "guild", None)
            if guild is not None:
                emoji = guild.get_emoji(emoji_id)
                if emoji is not None:
                    return emoji
            bot = getattr(self, "bot", None)
            if bot is not None:
                emoji = bot.get_emoji(emoji_id)
                if emoji is not None:
                    return emoji
            return discord.PartialEmoji(name="custom", id=emoji_id)
        return raw

    async def _maybe_add_scored_reaction(self, message: discord.Message, reaction: str | None) -> None:
        try:
            if getattr(message.author, "bot", False):
                return
            if not cfg_bool("EMOTION_REACTION_ENABLED", True):
                return
            normalized = _normalize_scored_reaction(reaction)
            if normalized == "NONE":
                return
            if normalized == "BAD":
                raw_content = str(getattr(message, "content", "") or "")
                if _is_friendly_or_greeting_message(raw_content):
                    log.info("suppressed BAD reaction for friendly/greeting user message: %r", raw_content[:60])
                    return
            now = time.time()
            cooldown_sec = max(cfg_float("EMOTION_REACTION_COOLDOWN_SEC", 45.0), 0.0)
            cid = getattr(getattr(message, "channel", None), "id", None) or 0
            last_ts = self._emotion_reaction_cooldowns.get(cid, 0.0)
            if (now - last_ts) < cooldown_sec:
                return
            resolved = self._resolve_scored_reaction_emoji(message, normalized)
            if resolved is None:
                return
            added = await self._safe_add_reaction(message, resolved)
            if added:
                self._emotion_reaction_cooldowns[cid] = now
                cache = getattr(self, "channel_context_cache", None)
                if cache is not None:
                    update_context_message_reaction(
                        cache,
                        cid,
                        getattr(message, "id", None),
                        resolved,
                        is_bot=True,
                    )
        except Exception as e:
            log.exception("scored reaction failed: %s", e)

    async def _bootstrap_emotion_base_name(self) -> None:
        if self._emotion_bootstrapped:
            return
        self._emotion_bootstrapped = True
        try:
            guild = self.bot.get_guild(cfg_int("GUILD_ID", 0))
            if guild is None:
                guild = await self.bot.fetch_guild(cfg_int("GUILD_ID", 0))
            me = guild.me if guild else None
            current_name = getattr(me, "display_name", None) or getattr(self.bot.user, "display_name", None) or getattr(self.bot.user, "name", None) or "Bot"
            configured = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
            self.emotion_state.base_display_name = configured or current_name
        except Exception:
            self.emotion_state.base_display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip() or "Bot"

    async def _sync_emotion_bio(self, emotion_key: str, reason: str) -> None:
        if not emotion_bio_enabled():
            return
        bio_text = build_emotion_bio_text(emotion_key, reason)
        bio_max_chars = max(cfg_int("EMOTION_BIO_DISCORD_MAX_CHARS", 190), 20)
        bio_text = truncate_text(bio_text, bio_max_chars)
        now = time.time()
        min_wait = cfg_float("EMOTION_BIO_UPDATE_MIN_SEC", 60.0)
        async with self._emotion_lock:
            if bio_text == self.emotion_state.last_applied_bio:
                return
            if (now - self.emotion_state.last_bio_update_ts) < min_wait:
                return
        route = discord.http.Route("PATCH", "/applications/@me")
        await self.bot.http.request(route, json={"description": bio_text})
        async with self._emotion_lock:
            self.emotion_state.last_applied_bio = bio_text
            self.emotion_state.last_bio_update_ts = time.time()

    async def _sync_emotion_presence(self) -> None:
        if not emotion_scoring_enabled() or not self.bot.user:
            return
        await self._bootstrap_emotion_base_name()
        async with self._emotion_lock:
            emotion_key = self.emotion_state.dominant_emotion or "neutral"
            reason = self.emotion_state.reason or "平常状態"
            base_name = self.emotion_state.base_display_name or getattr(self.bot.user, "name", "Bot")
            target_nick = build_nickname_with_face(base_name, emotion_key)
            target_activity = build_emotion_status_text(emotion_key, reason)
            now = time.time()
            nick_wait = cfg_float("EMOTION_NICKNAME_UPDATE_MIN_SEC", 20.0)
            activity_wait = cfg_float("EMOTION_ACTIVITY_UPDATE_MIN_SEC", 20.0)
            can_update_nick = (now - self.emotion_state.last_nickname_update_ts) >= nick_wait
            can_update_activity = (now - self.emotion_state.last_activity_update_ts) >= activity_wait
            skip_nick = target_nick == self.emotion_state.last_applied_nickname
            skip_activity = target_activity == self.emotion_state.last_applied_activity
        if can_update_nick and not skip_nick:
            try:
                target_guilds: list[discord.Guild] = []
                guild_id = cfg_int("GUILD_ID", 0)
                if guild_id:
                    g = self.bot.get_guild(guild_id)
                    if g:
                        target_guilds.append(g)
                if not target_guilds and getattr(self.bot, "guilds", None):
                    target_guilds = list(self.bot.guilds)
                updated_any = False
                for guild in target_guilds:
                    if guild and guild.me:
                        try:
                            await guild.me.edit(nick=target_nick)
                            updated_any = True
                        except Exception as e:
                            log.debug("guild nick edit failed for %s: %r", guild.id, e)
                if updated_any:
                    async with self._emotion_lock:
                        self.emotion_state.last_applied_nickname = target_nick
                        self.emotion_state.last_nickname_update_ts = time.time()
            except Exception as e:
                log.warning("nickname update failed: %r", e)
        if can_update_activity and not skip_activity:
            try:
                await self.bot.change_presence(activity=discord.CustomActivity(name=target_activity))
                async with self._emotion_lock:
                    self.emotion_state.last_applied_activity = target_activity
                    self.emotion_state.last_activity_update_ts = time.time()
            except Exception as e:
                log.warning("activity update failed: %r", e)
        try:
            await self._sync_emotion_bio(emotion_key, reason)
        except Exception as e:
            log.warning("bio update failed: %r", e)
