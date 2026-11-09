"""OllamaChatReplyCoreMixinのプロンプト構築・思考適応・口調ガードを担当するMixin。"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from harness.agent.prompt_parser.parser import habit_policy
from harness.agent.prompt_parser.prompt_manager import prospective_memory
from harness.agent.memory_manager.logic import get_user_habit_profile
from front.discord.common.discord_helpers import build_different_author_boundary_note
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    _relationship_score_for_habit,
    channel_id,
    extract_action_style_from_intent_info,
    model_supports_thinking,
    recent_context_items,
    truncate_text,
)
from ollama_bot.ollama_chat_.ollama_chat_texts import build_chat_reply_system_prompt
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState, MessageRuntime
from front.discord.reply.reply_core_common import (
    _OllamaChatReplyCoreBase,
    cfg,
    cfg_bool,
    cfg_int,
    log,
)


class ReplyPromptMixin(_OllamaChatReplyCoreBase):
    """プロンプト整形、思考適応判定、トークン数予測、アクションスタイル適応を担当するMixin。"""

    def _should_enable_adaptive_thinking(
        self,
        intent_info: dict[str, Any] | None,
        user_text: str = "",
    ) -> bool:
        model_name = str(cfg("OLLAMA_MODEL", "") or "").strip()
        try:
            can_think = bool(model_supports_thinking())
        except TypeError:
            try:
                can_think = bool(model_supports_thinking(model_name))  # type: ignore[call-arg]
            except Exception:
                can_think = True
        except Exception:
            can_think = True

        if not can_think:
            return False

        if intent_info:
            if intent_info.get("needs_reasoning") is True:
                return True
            if intent_info.get("adaptive_thinking") is True:
                return True
            intent = str(intent_info.get("intent") or "").strip()
            if intent in ("クイズ・パズル", "quiz", "puzzle", "クイズ", "パズル"):
                return True

        text = user_text or ""
        arrow_count = len(re.findall(r"(?:->|→)", text))
        has_multiline = "\n" in text.strip()
        if arrow_count >= 2 and has_multiline:
            return True

        puzzle_keywords = (
            "ヒント",
            "画数",
            "法則",
            "暗号",
            "◯に入る",
            "○に入る",
            "なぞなぞ",
            "クイズ",
            "パズル",
        )
        if any(kw in text for kw in puzzle_keywords):
            return True

        return False

    def _reply_num_predict(
        self,
        intent_info: dict[str, Any] | None = None,
        *,
        prompt_text: str = "",
        system_prompt: str = "",
        think: bool = False,
    ) -> int | None:
        if intent_info:
            rec = intent_info.get("recommended_num_predict")
            if isinstance(rec, int) and rec > 0:
                return rec

        needs_reasoning = bool(intent_info.get("needs_reasoning")) if intent_info else False
        is_think_active = think or cfg_bool("OLLAMA_MAIN_MODEL_THINK", False)

        if needs_reasoning or is_think_active:
            configured_think = cfg_int("OLLAMA_THINK_NUM_PREDICT", 2048)
            return max(configured_think, 1536)

        if intent_info:
            source = str(intent_info.get("source") or "").strip()
            if source in ("web", "research", "img", "x"):
                value = cfg_int("OLLAMA_REPLY_NUM_PREDICT_LONG", 1536)
                return value if value > 0 else 1536

        total_prompt_chars = len(prompt_text) + len(system_prompt)
        if total_prompt_chars >= 6000:
            value = cfg_int("OLLAMA_REPLY_NUM_PREDICT_LONG", 1536)
            return value if value > 0 else 1536

        default_num = 512
        value = cfg_int("OLLAMA_REPLY_NUM_PREDICT", default_num)
        return value if value > 0 else None

    def _reply_retry_num_predict(self, intent_info: dict[str, Any] | None = None) -> int | None:
        configured = cfg_int("OLLAMA_REPLY_RETRY_NUM_PREDICT", 0)
        if configured > 0:
            return configured
        base = self._reply_num_predict(intent_info) or 0
        return max(base * 2, 192) if base > 0 else 192

    def _reply_system_prompt(self) -> str:
        persona_blocks = [str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()]
        style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        if style_guard:
            persona_blocks.append("【口調ガード】\n" + style_guard)
        if extra_rule:
            persona_blocks.append("【追加ルール】\n" + extra_rule)
        persona = "\n\n".join(block for block in persona_blocks if block)
        return build_chat_reply_system_prompt(persona)

    def _clean_duplicate_prompt_instructions(self, prompt: str, system_prompt: str) -> str:
        """システムプロンプトと重複する口調ガード・追加ルールをユーザープロンプトから除去してトークンを削減する。"""
        if not prompt or not system_prompt:
            return prompt

        style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()

        cleaned = prompt

        # 1. 【最終口調ガード】/【口調ガード】の重複除去
        if style_guard and style_guard in system_prompt:
            for header in ("【最終口調ガード】", "【口調ガード】"):
                pattern = re.compile(rf"\n*{re.escape(header)}\s*\n+{re.escape(style_guard)}(?=\n|\Z)")
                cleaned = pattern.sub("", cleaned)
            if style_guard in cleaned:
                cleaned = cleaned.replace(style_guard, "")

        # 2. 【最終追加ルール】/【追加ルール】の重複除去
        if extra_rule and extra_rule in system_prompt:
            for header in ("【最終追加ルール】", "【追加ルール】"):
                pattern = re.compile(rf"\n*{re.escape(header)}\s*\n+{re.escape(extra_rule)}(?=\n|\Z)")
                cleaned = pattern.sub("", cleaned)
            if extra_rule in cleaned:
                cleaned = cleaned.replace(extra_rule, "")

        # 3. 見出しブロック内のテキストがシステムプロンプトに含まれる場合の汎用除去
        for header in ("【最終口調ガード】", "【口調ガード】", "【最終追加ルール】", "【追加ルール】"):
            if header in cleaned:
                match = re.search(rf"\n*{re.escape(header)}\s*\n+([^\n]+(?:\n[^\n]+)*?)(?=\n\n|\Z)", cleaned)
                if match:
                    block_body = match.group(1).strip()
                    if block_body and block_body in system_prompt:
                        cleaned = cleaned[:match.start()] + cleaned[match.end():]

        # 4. 連続空行の整理
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    def _format_reply_deliberation_note(self, result: dict[str, Any], *, trigger_reason: str) -> str:
        trigger_map = {
            "anger": "怒りが強く出ている",
            "surprise": "驚きが強く出ている",
            "curiosity": "好奇心が高まっている",
            "classifier": "分類器が熟考パスを推奨した",
        }
        max_chars = cfg_int("OLLAMA_DELIBERATION_MAX_CHARS", 120)
        items = [
            ("intent", "意図"),
            ("topic_origin", "話題の起点・背景"),
            ("emotion_focus", "感情の軸"),
            ("memory_hook", "拾う記憶"),
            ("reply_strategy", "返答方針"),
            ("avoid", "避けること"),
            ("consistency_correction", "キャラ維持指示"),
        ]

        lines = [
            "※内部整理用。以下の箇条書きや見出しは最終返答に出力せず、内容だけ自然に反映してください。"
        ]
        for key, label in items:
            value = truncate_text(str(result.get(key) or "").strip(), max_chars)
            if value:
                lines.append(f"- {label}: {value}")

        trigger_text = trigger_map.get(trigger_reason, "").strip()
        if trigger_text:
            lines.append(f"- 熟考トリガー: {trigger_text}")

        if len(lines) <= 1:
            return ""

        return "[返答前の内省メモ]\n" + "\n".join(lines) + "\n[/返答前の内省メモ]"

    async def _select_action_style_for_turn(
        self,
        *,
        runtime: MessageRuntime,
        intent_info: dict[str, Any],
        deliberation_info: dict[str, Any],
        working_context: dict[str, Any],
        emotion_state: EmotionState | None,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> tuple[str, str]:
        relationship_snapshot = await self._get_user_relationship_snapshot(
            getattr(runtime, "user_id", None),
            extra_scores=incoming_emotion_scores,
            preview_with_extra_scores=True,
        )
        relationship_score = _relationship_score_for_habit(relationship_snapshot)
        context_key = habit_policy.build_context_key(
            user_id=getattr(runtime, "user_id", None),
            channel_id=getattr(runtime, "cid", None),
            topic=str((working_context or {}).get("topic", "") or ""),
            relationship_score=relationship_score,
            emotion_state=emotion_state,
        )
        base_style = extract_action_style_from_intent_info(intent_info, deliberation_info)
        allowed_styles = habit_policy.get_allowed_styles(cfg)
        habit_bonus = await habit_policy.get_habit_bonus(
            self.memory_store,
            persona=cfg_persona_namespace(custom_cfg=cfg),
            context_key=context_key,
        )
        user_profile = await get_user_habit_profile(
            cfg=cfg,
            store=self.memory_store,
            guild_id=getattr(runtime, "guild_id", None),
            user_id=getattr(runtime, "user_id", None),
        )
        for style, bonus in habit_policy.action_style_bonus_from_user_profile(
            user_profile,
            allowed_styles=allowed_styles,
        ).items():
            habit_bonus[style] = max(float(habit_bonus.get(style, 0.0) or 0.0), float(bonus))
        selected_style = habit_policy.rerank_with_habit(
            style=base_style,
            habit_bonus=habit_bonus,
            allowed_styles=allowed_styles,
            habit_weight=habit_policy.get_habit_weight(cfg),
        )
        return selected_style, context_key

    def _build_action_style_prompt_block(self, action_style: str) -> str:
        if not action_style:
            return ""
        instruction = habit_policy.build_action_style_instruction(action_style)
        return (
            "[今回の返答スタイル]\n"
            f"- action_style: {action_style}\n"
            f"- 指示: {instruction}"
        )

    async def _mark_future_prompts_triggered(self, runtime: MessageRuntime) -> None:
        for record in getattr(runtime, "pending_future_prompts", []) or []:
            record_id = record.get("id")
            if record_id is None:
                continue
            try:
                await prospective_memory.mark_triggered(self.memory_store, int(record_id))
            except Exception as e:
                log.warning("prospective mark_triggered failed: id=%s err=%r", record_id, e)

    def _remember_pending_habit_turn(
        self,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> None:
        if not getattr(runtime, "selected_action_style", "") or not getattr(runtime, "habit_context_key", ""):
            return
        key = (getattr(runtime, "cid", None), getattr(runtime, "user_id", None))
        self._pending_habit_turns[key] = {
            "persona": cfg_persona_namespace(custom_cfg=cfg),
            "context_key": runtime.habit_context_key,
            "action_style": runtime.selected_action_style,
            "emotion_scores": dict(incoming_emotion_scores or {}),
            "used_at": time.time(),
        }

    async def _apply_action_style_to_prompt(
        self,
        prompt: str,
        *,
        runtime: MessageRuntime,
        intent_info: dict[str, Any],
        deliberation_info: dict[str, Any],
        working_context: dict[str, Any],
        emotion_state: EmotionState | None,
        pending_future_prompts: list[dict[str, Any]] | None = None,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> str:
        action_style, context_key = await self._select_action_style_for_turn(
            runtime=runtime,
            intent_info=intent_info,
            deliberation_info=deliberation_info,
            working_context=working_context,
            emotion_state=emotion_state,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        runtime.selected_action_style = action_style
        runtime.habit_context_key = context_key
        runtime.working_context = dict(working_context or {})
        runtime.pending_future_prompts = list(pending_future_prompts or [])

        prompt_block = self._build_action_style_prompt_block(action_style)
        if prompt_block:
            return f"{prompt_block}\n\n{prompt}"
        return prompt

    def _prepend_social_guidance_to_prompt(self, prompt: str, social_guidance: str) -> str:
        """Prepend social guidance (emotions/relationships) to the prompt."""
        return (
            "[現在の感情状態と相手との関係性]\n"
            f"{social_guidance}\n\n"
            f"{prompt}"
        )

    def _apply_different_author_boundary_note(
        self,
        prompt: str,
        message: discord.Message,
        last_history_message: Any = None,
    ) -> str:
        """直前に別ユーザーの発言が存在する場合に境界注記をプロンプトへ注入する。"""
        prior_items: list[dict[str, Any]] | None = None
        if last_history_message is None:
            cid = channel_id(message)
            if cid is not None and getattr(self, "channel_context_cache", None):
                q = self.channel_context_cache.get(cid)
                if q:
                    cached_items = recent_context_items(self.channel_context_cache, cid, before_message=message)
                    last_idx = -1
                    for idx in range(len(cached_items) - 1, -1, -1):
                        cand = cached_items[idx]
                        if isinstance(cand, dict):
                            cand_role = str(cand.get("role") or "").strip().lower()
                            cand_name = str(cand.get("name") or "").strip().lower()
                            cand_line = str(cand.get("line") or "").strip()
                            if (
                                cand_role == "system"
                                or cand_name == "system"
                                or cand_line.startswith(("System:", "(内心のメモ", "（内心のメモ"))
                            ):
                                continue
                        last_idx = idx
                        last_history_message = cand
                        break
                    if last_idx > 0:
                        prior_items = [
                            it for it in cached_items[:last_idx]
                            if not (
                                isinstance(it, dict)
                                and (
                                    str(it.get("role") or "").strip().lower() == "system"
                                    or str(it.get("name") or "").strip().lower() == "system"
                                    or str(it.get("line") or "").strip().startswith(("System:", "(内心のメモ", "（内心のメモ"))
                                )
                            )
                        ]

        bot_uid = self.bot.user.id if getattr(self, "bot", None) and getattr(self.bot, "user", None) else None
        note = build_different_author_boundary_note(
            message,
            last_history_message,
            bot_user_id=bot_uid,
            prior_context_items=prior_items,
        )
        if note and note not in prompt:
            return f"{prompt}\n\n{note}"
        return prompt
