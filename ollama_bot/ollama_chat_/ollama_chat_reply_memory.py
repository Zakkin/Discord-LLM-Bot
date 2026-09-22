"""OllamaChatReplyMixinの長期記憶、エピソード記憶、会話要約更新を担当する補助Mixin。

このファイルは返信後にユーザー発言とBot返答を記憶候補へ変換し、
チャンネル要約、習慣プロファイル、エピソード記憶、未来プロンプトの消化状態を更新する。
返答生成そのものやDiscord送信処理は別のreply系Mixinに分離している。
"""
import asyncio
import logging
import sys
from typing import Any, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr


import discord

from ..common.discord_helpers import format_message_reactions, author_id
from ..common.emotion_helpers import merge_emotion_scores
from ..common.memory_logic import (
    build_memory_emotion_tags,
)
from ..common.ollama_helpers import (
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    looks_like_reasoning_leak,
    truncate_lines,
    truncate_text,
)
from . import ollama_chat_helpers as _helpers
from .ollama_chat_types import MessageRuntime

log = logging.getLogger("ollama_bot.ollama_chat_.reply_memory")

_PUBLIC_REPLY_MODULE = f"{__package__}.ollama_chat_reply"
_default_cfg = _helpers.cfg
_default_cfg_bool = _helpers.cfg_bool
_default_extract_and_store_memory = _helpers.extract_and_store_memory
_default_maybe_store_training_candidate = _helpers.maybe_store_training_candidate
_default_update_user_habit_profile = _helpers.update_user_habit_profile
_default_update_user_interests = _helpers.update_user_interests
_default_build_summary_context_lines = _helpers._build_summary_context_lines
_default_maybe_update_channel_summary = _helpers.maybe_update_channel_summary


def _public_attr(name: str, default: Any = None) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)



def cfg(name: str, default: Any = None) -> Any:
    return _public_attr("cfg", _default_cfg)(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    return _public_attr("cfg_bool", _default_cfg_bool)(name, default)


def extract_and_store_memory(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("extract_and_store_memory", _default_extract_and_store_memory)(*args, **kwargs)


def maybe_store_training_candidate(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("maybe_store_training_candidate", _default_maybe_store_training_candidate)(*args, **kwargs)


def update_user_habit_profile(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("update_user_habit_profile", _default_update_user_habit_profile)(*args, **kwargs)


def update_user_interests(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("update_user_interests", _default_update_user_interests)(*args, **kwargs)


def _build_summary_context_lines(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("_build_summary_context_lines", _default_build_summary_context_lines)(*args, **kwargs)


def maybe_update_channel_summary(*args: Any, **kwargs: Any) -> Any:
    return _public_attr("maybe_update_channel_summary", _default_maybe_update_channel_summary)(*args, **kwargs)


if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChatReplyMemoryBase = OllamaChatProtocol
else:
    _OllamaChatReplyMemoryBase = object


class OllamaChatReplyMemoryMixin(_OllamaChatReplyMemoryBase):

    def _episode_importance_score(
        self,
        *,
        user_text: str,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> float:
        peak = max((float(v or 0.0) for v in (incoming_emotion_scores or {}).values()), default=0.0)
        text = str(user_text or "").strip()
        reply = str(assistant_text or "").strip()
        importance = peak * 0.7
        if len(text) >= 30:
            importance += 0.08
        if any(marker in text for marker in ("助けて", "つらい", "しんどい", "怖い", "やばい", "ごめん", "ありがとう", "嬉しい", "かなしい", "悲しい")):
            importance += 0.12
        if any(marker in reply for marker in ("大丈夫", "落ち着", "まず", "なら", "してみ", "確認")):
            importance += 0.08
        return max(0.0, min(1.0, importance))

    def _episode_source_label(self, source: str, research_result: dict[str, Any] | None = None) -> str:
        mode = str((research_result or {}).get("mode") or source or "").strip().lower()
        if mode == "x_timeline":
            return "Xタイムライン"
        if mode == "img_top5":
            return "IMG勢い上位"
        if mode == "img_thread":
            return "IMGスレッド"
        if mode in {"browser_read_url", "browser_search", "simple_search"}:
            return "Web調査"
        if mode == "x":
            return "Xタイムライン"
        if mode == "img":
            return "IMG勢い上位"
        return "Discord"

    def _summarize_bot_action_for_episode(self, runtime: MessageRuntime, assistant_text: str) -> str:
        action_style = str(getattr(runtime, "selected_action_style", "") or "").strip()
        style_map = {
            "tease_then_answer": "軽くツッコミを入れてから答えた",
            "give_steps_first": "結論や手順を先に示した",
            "short_reaction": "短く反応した",
            "direct_reply": "率直に答えた",
            "validate_first": "まず受け止めてから答えた",
            "clarify_gently": "やわらかく確認しながら返した",
        }
        if action_style in style_map:
            return style_map[action_style]
        cleaned = truncate_text(str(assistant_text or "").strip(), 80)
        return cleaned or "返答した"

    def _build_episode_what_text(
        self,
        *,
        runtime: MessageRuntime,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> str:
        parts: list[str] = []
        user_text = truncate_text(str(runtime.original_user_text or "").strip(), 220)
        if user_text:
            parts.append(f"Discord発言: {user_text}")

        result = dict(research_result or {})
        context = dict(episode_context or {})
        query = str(result.get("query") or context.get("source_query") or "").strip()
        if query:
            parts.append(f"参照先: {truncate_text(query, 180)}")

        source_summary = str(result.get("summary") or context.get("source_summary") or "").strip()
        if source_summary:
            parts.append(f"参照内容: {truncate_text(source_summary, 420)}")

        if not parts and source:
            parts.append(f"source={source}")
        return "\n".join(parts)[:600]

    async def _maybe_store_episode_memory(
        self,
        *,
        message: discord.Message,
        runtime: MessageRuntime,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> None:
        if not cfg_bool("MEMORY_EPISODE_ENABLED", True):
            return
        add_episode = getattr(self.memory_store, "add_episode", None)
        if not callable(add_episode):
            return

        importance = self._episode_importance_score(
            user_text=runtime.original_user_text,
            assistant_text=assistant_text,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        source_label = self._episode_source_label(source, research_result)
        if cfg_bool("MEMORY_EPISODE_STORE_ALL_TURNS", True):
            importance = max(importance, float(cfg("MEMORY_EPISODE_BASE_IMPORTANCE", 0.42) or 0.42))
        elif importance < float(cfg("MEMORY_EPISODE_STORE_THRESHOLD", 0.62) or 0.62):
            return

        emotion_tags = build_memory_emotion_tags(incoming_emotion_scores or {}, max_tags=3, min_score=0.30)
        user_text = truncate_text(runtime.original_user_text, 120)
        action_summary = self._summarize_bot_action_for_episode(runtime, assistant_text)
        _, reactions_list = format_message_reactions(message, bot_user_id=runtime.bot_user_id)
        bot_reacts = [r for r in reactions_list if r.get("is_bot")]
        if bot_reacts:
            react_names = "・".join(r["emoji"] for r in bot_reacts)
            action_summary = f"{react_names}でリアクションし、{action_summary}"
        if source_label == "Discord":
            summary = f"{user_text} に対して、{action_summary}。".strip()
        else:
            summary = f"{source_label}を見て、{user_text or 'Discord上の流れ'} に対して、{action_summary}。".strip()
        tags = list(emotion_tags)
        source_tag = str((research_result or {}).get("mode") or source or "discord").strip().lower() or "discord"
        for tag in ("episode", source_tag):
            if tag not in tags:
                tags.append(tag)

        bot_action_text = truncate_text(assistant_text or action_summary, 600)
        if bot_reacts and not any(r["emoji"] in bot_action_text for r in bot_reacts):
            bot_action_text = f"[{' '.join(r['emoji'] for r in bot_reacts)}] {bot_action_text}"

        await add_episode(
            persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            summary=summary,
            what=self._build_episode_what_text(
                runtime=runtime,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            ),
            bot_action=bot_action_text,
            user_reaction=None,
            emotion_before=incoming_emotion_scores or None,
            emotion_after=None,
            importance=importance,
            tags=tags,
        )

    async def _store_and_update_summary(
        self,
        message: discord.Message,
        sent: discord.Message,
        *,
        runtime: MessageRuntime,
        context_lines: list[str],
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None = None,
        spontaneous_source: str | None = None,
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> None:
        if looks_like_abnormal_assistant_reply(assistant_text):
            return
        skip_long_term_memory = str(spontaneous_source or "").strip().lower() == "x"
        source = (
            str((research_result or {}).get("mode") or "").strip()
            or str(spontaneous_source or "").strip()
            or "discord"
        )

        trimmed_context_lines = truncate_lines(
            context_lines,
            max_lines=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
            max_chars_per_line=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
            max_total_chars=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
        )
        memory_context_lines = self._trim_memory_context_lines(
            runtime.memory_context_lines or context_lines
        ) or trimmed_context_lines

        emotion_scores, _, emotion_reason = await self._get_emotion_snapshot()
        emotion_scores = merge_emotion_scores(emotion_scores, incoming_emotion_scores)

        if not skip_long_term_memory:
            try:
                await extract_and_store_memory(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    emotion_scores=emotion_scores,
                    emotion_reason=emotion_reason,
                )
            except Exception as e:
                log.exception("memory extract failed: %s", e)

        try:
            await self._maybe_store_episode_memory(
                message=message,
                runtime=runtime,
                assistant_text=assistant_text,
                incoming_emotion_scores=incoming_emotion_scores,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            )
        except Exception as e:
            log.exception("episode memory store failed: %s", e)

        highest_emotion_score = max(emotion_scores.values()) if emotion_scores else 0.0
        if highest_emotion_score >= 0.8:
            _reflection_task = asyncio.create_task(self._run_deep_reflection_once(
                channel_id=runtime.cid,
                emotion_scores=emotion_scores,
                emotion_reason=emotion_reason or "不明",
            ))
            _reflection_task.add_done_callback(
                lambda t: log.warning("deep reflection task failed: %r", t.exception()) if t.exception() else None
            )

        candidate_saved = False
        if not skip_long_term_memory:
            try:
                candidate_saved = await maybe_store_training_candidate(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=trimmed_context_lines,
                    looks_like_reasoning_leak=looks_like_reasoning_leak,
                    looks_like_parrot_reply=looks_like_parrot_reply,
                    is_weather_reply=runtime.is_weather_reply,
                )
            except Exception as e:
                log.exception("training candidate save failed: %s", e)
                candidate_saved = False

        if candidate_saved:
            try:
                await update_user_habit_profile(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    candidate_reason="auto-saved training candidate",
                )
            except Exception as e:
                log.exception("habit profile update failed: %s", e)

        if not skip_long_term_memory:
            try:
                await update_user_interests(
                    cfg=cfg,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    context_lines=memory_context_lines,
                )
            except Exception as e:
                log.exception("user interests update failed: %s", e)

        try:
            summary_context_lines = await _build_summary_context_lines(
                message,
                sent,
                base_context_lines=memory_context_lines,
                bot_user_id=runtime.bot_user_id,
                fetch_limit=max(
                    int(cfg("MEMORY_SUMMARY_CONTEXT_LINES", cfg("MEMORY_LONG_CONTEXT_LINES", 30)) or 30),
                    int(cfg("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20),
                ),
            )
        except Exception as e:
            log.exception("summary context build failed: %s", e)
            summary_context_lines = memory_context_lines

        try:
            await maybe_update_channel_summary(
                cfg=cfg,
                store=self.memory_store,
                guild_id=getattr(message.guild, "id", None),
                channel_id=runtime.cid,
                context_lines=summary_context_lines,
            )
        except Exception as e:
            log.exception("channel summary update failed: %s", e)

        try:
            await self._mark_future_prompts_triggered(runtime)
        except Exception as e:
            log.exception("prospective trigger update failed: %s", e)

        # 進行中の会話スレッド状態を長期記憶DBへ永続化
        if hasattr(self, "thread_tracker") and self.thread_tracker and runtime.cid is not None:
            save_thread = getattr(self.memory_store, "save_channel_thread", None)
            if callable(save_thread):
                try:
                    persona_ns = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default")
                    active_threads = self.thread_tracker.get_active_threads(runtime.cid)
                    for th in active_threads:
                        await save_thread(
                            persona=persona_ns,
                            guild_id=getattr(message.guild, "id", None),
                            channel_id=runtime.cid,
                            thread_key=th.thread_id,
                            topic=th.topic,
                            participants=th.participant_names,
                            turns=list(th.turns),
                            last_active_ts=th.last_active_ts,
                        )
                except Exception as e:
                    log.exception("channel threads persistence failed: %s", e)

        if not skip_long_term_memory:
            self._remember_pending_habit_turn(runtime, incoming_emotion_scores=incoming_emotion_scores)
