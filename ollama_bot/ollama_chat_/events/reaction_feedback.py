"""リアクション付与・削除、フィードバック記録、感情更新タスク管理を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Optional, TYPE_CHECKING

import discord

from lib.config_utils import cfg, cfg_float, cfg_persona_namespace
from lib.discord_utils import author_id, channel_id, content
from ..ollama_chat_helpers import _infer_feedback_type, looks_like_feedback_text
from harness.agent.prompt_parser.prompt_manager.history_helpers import safe_message_content, find_recent_bot_and_user_pair
from ..ollama_chat_types import MessageRuntime

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _ReactionFeedbackBase = OllamaChatProtocol
else:
    _ReactionFeedbackBase = object

log = logging.getLogger("ollama_bot.ollama_chat")


class _ReactionFeedbackMixin(_ReactionFeedbackBase):
    """リアクション安全操作、フィードバック永続化、感情更新スケジューリングを提供するMixin。"""

    def _emotion_prereply_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PREREPLY_TIMEOUT_SEC", 2.0), 0.5)

    def _emotion_presence_sync_timeout_sec(self) -> float:
        return max(cfg_float("EMOTION_PRESENCE_SYNC_TIMEOUT_SEC", 15.0), 5.0)

    def _emotion_update_timeout_sec(self, *, needs_scoring: bool) -> float:
        configured = cfg_float("EMOTION_UPDATE_TIMEOUT_SEC", 0.0)
        if configured > 0.0:
            return configured
        reason_timeout = max(cfg_float("EMOTION_REASON_TIMEOUT_SEC", 20.0), 5.0)
        if needs_scoring:
            score_timeout = max(
                cfg_float("EMOTION_SCORE_TIMEOUT_SEC", cfg_float("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0)),
                10.0,
            )
            return score_timeout + reason_timeout + 5.0
        return reason_timeout + 5.0

    def _is_reaction_blocked_error(self, exc: Exception) -> bool:
        if not isinstance(exc, discord.Forbidden):
            return False
        code = getattr(exc, "code", None)
        if code == 90001:
            return True
        return "Reaction blocked" in str(exc)

    async def _safe_add_reaction(self, message: discord.Message, emoji: str) -> bool:
        try:
            await message.add_reaction(emoji)
            return True
        except Exception as e:
            if self._is_reaction_blocked_error(e):
                log.info(
                    "reaction blocked; skip add_reaction channel=%s message=%s emoji=%r",
                    channel_id(message), getattr(message, "id", None), emoji
                )
                return False
            log.exception("add reaction failed: %s", e)
            return False

    async def _safe_remove_own_reaction(self, message: discord.Message, emoji: str) -> bool:
        if not self.bot.user:
            return False
        try:
            await message.remove_reaction(emoji, self.bot.user)
            return True
        except Exception as e:
            if self._is_reaction_blocked_error(e):
                log.info(
                    "reaction blocked; skip remove_reaction channel=%s message=%s emoji=%r",
                    channel_id(message), getattr(message, "id", None), emoji
                )
                return False
            log.exception("remove reaction failed: %s", e)
            return False

    async def _maybe_capture_feedback(self, message: discord.Message, *, bot_user_id: Optional[int]) -> None:
        if bot_user_id is None or getattr(message.author, "bot", False):
            return
        feedback_text = content(message)
        if not looks_like_feedback_text(feedback_text):
            return

        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id = getattr(message.guild, "id", None)
        cid = channel_id(message)
        uid = author_id(message)
        try:
            bot_msg, prev_user_msg = await find_recent_bot_and_user_pair(
                message,
                bot_user_id=bot_user_id,
                limit=12,
            )
        except Exception as e:
            log.exception("feedback history fetch failed: %s", e)
            return

        if bot_msg is None:
            return

        assistant_text = safe_message_content(bot_msg)
        if not assistant_text:
            return

        try:
            feedback_id = await self.memory_store.add_feedback_item(
                persona=persona,
                guild_id=guild_id,
                channel_id=cid,
                user_id=uid,
                original_user_text=safe_message_content(prev_user_msg),
                assistant_text=assistant_text,
                feedback_text=feedback_text,
                corrected_assistant_text=None,
                feedback_type=_infer_feedback_type(feedback_text),
                accepted=False,
            )
            log.info("feedback item saved: id=%s persona=%s channel=%s user=%s", feedback_id, persona, cid, uid)
        except Exception as e:
            log.exception("feedback save failed: %s", e)

    def _schedule_emotion_update(
        self,
        message: discord.Message,
        *,
        runtime: MessageRuntime | None = None,
        incoming_scores: dict[str, float] | None = None,
        incoming_appraisal: str | None = None,
        incoming_reaction: str | None = None,
    ) -> None:
        try:
            task = asyncio.create_task(
                self._run_emotion_update_for_message(
                    message,
                    runtime=runtime,
                    incoming_scores=incoming_scores,
                    incoming_appraisal=incoming_appraisal,
                    incoming_reaction=incoming_reaction,
                )
            )
            log.info("TRACE emotion: task scheduled channel=%s author=%s", channel_id(message), author_id(message))

            def _done_callback(t: asyncio.Task[Any]) -> None:
                with contextlib.suppress(asyncio.CancelledError):
                    exc = t.exception()
                    if exc is not None:
                        log.exception("TRACE emotion: task failed channel=%s author=%s err=%r", channel_id(message), author_id(message), exc)

            task.add_done_callback(_done_callback)
        except Exception as e:
            log.exception("TRACE emotion: task schedule failed channel=%s author=%s err=%r", channel_id(message), author_id(message), e)
