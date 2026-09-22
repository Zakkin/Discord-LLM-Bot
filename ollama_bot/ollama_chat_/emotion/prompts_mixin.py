"""
[AI Agent Summary]
このファイルは、LLMに感情の評価・理由づけを行わせるためのプロンプト生成や、ユーザー関係性に基づくプロンプト生成を担当する Mixin です。
This mixin handles the generation of prompts for the LLM to evaluate emotions, reason about them, and generate social/emotional guidance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from typing import Any

from lib.date_utils import now_jst


import discord

from ...common import memory_logic
from ...common.config_helpers import cfg, cfg_bool, cfg_float, cfg_int
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    EMOTION_LABEL_TO_FACE,
    EMOTION_LABEL_TO_JA,
    apply_emotion_persona_weights,
    build_memory_emotion_tags,
    build_emotion_evaluation_sections,
    build_emotion_bio_text,
    build_emotion_status_text,
    build_nickname_with_face,
    build_reply_emotion_guidance,
    clamp_score,
    contains_cjk_non_japanese,
    emotion_bio_enabled,
    emotion_scoring_enabled,
    fallback_reason_text,
    looks_like_bad_reason_text,
    merge_emotion_scores,
    normalize_llm_emotion_scores,
    normalize_reason_text,
)
from ...common.ollama_helpers import call_ollama_json, truncate_lines, truncate_text

from ..ollama_chat_texts import (
    EMOTION_REASONER_SYSTEM_PROMPT,
    build_emotion_scorer_system_prompt,
)
from ..ollama_chat_types import MessageRuntime, UserRelationship

log = logging.getLogger("ollama_bot.ollama_chat")


from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionPromptsBase = OllamaChatProtocol
else:
    _EmotionPromptsBase = object

from .relationships_logic import build_relationship_prompt, _apply_relationship_delta, _copy_relationship


class OllamaChatEmotionPromptsMixin(_EmotionPromptsBase):
    def _derive_mood_from_state(self) -> tuple[str, str]:
        scores = self.emotion_state.scores or {}
        dominant = self.emotion_state.dominant_emotion or "neutral"
        boredom = clamp_score(float(self.emotion_state.boredom or 0.0))
        loneliness = clamp_score(float(self.emotion_state.loneliness or 0.0))
        curiosity = clamp_score(float(self.emotion_state.curiosity or 0.0))
        fatigue = clamp_score(float(getattr(self.emotion_state, 'fatigue', 0.0) or 0.0))
        tension = clamp_score(float(getattr(self.emotion_state, 'tension', 0.0) or 0.0))
        excitement = clamp_score(float(getattr(self.emotion_state, 'excitement', 0.0) or 0.0))

        if fatigue >= 0.75:
            return "fatigued", "疲労がかなり蓄積しているため"
        if tension >= 0.75:
            return "tense", "極度の緊張・警戒状態にあるため"
        if excitement >= 0.75:
            return "excited", "気分が高揚しているため"

        if dominant in {"anger", "disgust"} and max(float(scores.get(dominant, 0.0)), 0.0) >= 0.25:
            return "irritated", "不快寄りの反応が続いているため"
        if dominant in {"sadness", "fear"} and max(float(scores.get(dominant, 0.0)), 0.0) >= 0.25:
            return "gloomy", "沈み気味の反応が続いているため"
        if curiosity >= 0.65:
            return "curious", "気になる話題が溜まっているため"
        if boredom >= 0.75 and loneliness >= 0.50:
            return "restless", "退屈と手持ち無沙汰が強まっているため"
        if dominant in {"joy", "anticipation", "surprise"} and max(float(scores.get(dominant, 0.0)), 0.0) >= 0.25:
            return "upbeat", "前向きな反応がやや優勢なため"
        return "neutral", "大きな偏りがないため"

    def _build_time_rhythm_guidance(self) -> str:
        if not cfg_bool("EMOTION_TIME_AWARE_PROMPT_ENABLED", True):
            return ""
        current_jst = now_jst()
        hour = current_jst.hour
        minute = current_jst.minute

        if 0 <= hour <= 4:
            tone = "現在は深夜です。少し眠たそうで、感情が内向きになりやすい時間帯です。返答は静かめで、少しだけ気だるさを混ぜてください。"
            contradiction_rule = "【時間帯整合性】現在は深夜です。朝や日中と決めつけた描写・挨拶（『朝から』『おはよう』『こんにちは』等）は絶対に言わないでください。『起きろ』等と言われても朝と誤認せず、『こんな深夜に…』等と現在の深夜に合わせて反応してください。"
        elif 5 <= hour <= 10:
            tone = "現在は朝です。まだ完全には調子が上がりきっていない前提で、返答は少し低速で控えめにしてください。"
            contradiction_rule = "【時間帯整合性】現在は朝です。夜や深夜と決めつけた挨拶（『こんばんは』『おやすみ』等）は避けてください。"
        elif 11 <= hour <= 17:
            tone = "現在は日中です。返答はもっとも安定しやすい時間帯として、自然で素直な調子を優先してください。"
            contradiction_rule = "【時間帯整合性】現在は日中です。朝や夜と決めつけた挨拶（『おはよう』『こんばんは』『おやすみ』等）は避けてください。"
        elif 18 <= hour <= 22:
            tone = "現在は夜です。返答は少し感情が出やすく、柔らかさや余韻がにじむ程度にしてください。"
            contradiction_rule = "【時間帯整合性】現在は夜です。朝や日中と決めつけた描写・挨拶（『朝から』『おはよう』『こんにちは』『朝っぱら』等）は絶対に言わないでください。『起きろ』等と言われても朝と誤認せず、『こんな夜に…』『もう夜ですよ』等と現在の夜に合わせて反応してください。"
        else:
            tone = "現在はかなり遅い時間です。返答は少し疲れと感傷が混じるくらいの抑えた調子にしてください。"
            contradiction_rule = "【時間帯整合性】現在はかなり遅い夜です。朝や日中と決めつけた描写・挨拶（『朝から』『おはよう』等）は絶対に言わないでください。『起きろ』等と言われても朝と誤認せず、現在の夜に合わせて反応してください。"
        return f"現在時刻は日本時間で{hour}時{minute}分です。{tone}\n{contradiction_rule}"

    async def _get_emotion_snapshot(self) -> tuple[dict[str, float], str, str]:
        async with self._emotion_lock:
            scores = dict(self.emotion_state.scores or {})
            dominant = self.emotion_state.dominant_emotion or "neutral"
            reason = self.emotion_state.reason or "うまく言葉にできない状態です。"
        return scores, dominant, reason

    async def _get_user_relationship_snapshot(
        self,
        user_id: int | None,
        *,
        extra_scores: dict[str, float] | None = None,
        preview_with_extra_scores: bool = False,
    ) -> UserRelationship | None:
        if user_id is None:
            return None
        self._ensure_relationship_state()
        async with self._relationship_lock:
            snapshot = _copy_relationship(self.user_relationships.get(int(user_id)))
        if preview_with_extra_scores and extra_scores:
            snapshot = _apply_relationship_delta(snapshot, extra_scores)
        return snapshot

    async def _build_relationship_prompt_for_user(
        self,
        user_id: int | None,
        *,
        extra_scores: dict[str, float] | None = None,
        preview_with_extra_scores: bool = False,
        instruction: str = "",
    ) -> str:
        relationship = await self._get_user_relationship_snapshot(
            user_id,
            extra_scores=extra_scores,
            preview_with_extra_scores=preview_with_extra_scores,
        )
        if relationship is None:
            return ""
        return build_relationship_prompt(relationship, instruction=instruction)

    def _build_emotion_prompt(
        self,
        runtime: MessageRuntime,
        *,
        current_scores: dict[str, float] | None = None,
        current_dominant: str = "neutral",
        current_appraisal: str = "大きな出来事とは受け取っていない。",
        current_reason: str = "平常状態",
        memory_lines: list[str] | None = None,
        relationship_prompt: str = "",
    ) -> str:
        target_text = (runtime.effective_user_text or runtime.original_user_text or "").strip() or "（本文なし）"
        self_label = "あなた"
        self_aliases = self._emotion_self_aliases()
        ranked_current = sorted(
            (
                (key, clamp_score((current_scores or {}).get(key, 0.0)))
                for key in EMOTION_KEYS
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        active_current = [(key, score) for key, score in ranked_current if score >= 0.05]
        parts = [
            "これは対話AI本人の感情判定です。",
            "会話中の『相手』と『あなた』を混同せず、相手の感情ではなく、相手の発言を受けたあなた自身の内的反応だけを評価してください。",
            "相手が強い感情を表明していても、その感情をそのまま写さず、あなたが実際にどう反応するかを数値化してください。",
            "",
        ]
        parts.append("現在の持ち越し感情:")
        if active_current:
            for key, score in active_current[:3]:
                parts.append(f"- {EMOTION_LABEL_TO_JA.get(key, key)}: {score:.2f}")
        else:
            parts.append("- 特に強い偏りはない")
        parts.append(f"現在の主要感情: {EMOTION_LABEL_TO_JA.get(current_dominant, current_dominant)}")
        parts.append(f"現在の出来事評価（前回の発言に対する評価メモ）: {current_appraisal or '大きな出来事とは受け取っていない。'}")
        parts.append(f"現在の理由: {current_reason or '平常状態'}")
        parts.append("この持ち越し感情は会話の余韻です。新しい発言だけで機械的に全感情を0に戻さず、残っている余韻があれば低めの値でも反映してください。")
        parts.append("※重要: 『現在の出来事評価』は前回の発言に対する過去のメモです。今回の新しい発言に対して前回の評価をそのままコピー・模倣せず、今回の発言そのものから独立してappraisal（出来事評価）を決定してください。")
        parts.append("※重要: 相手からの日常的な質問（好み、趣味、日常会話、挨拶等）を『自分の感情を無視された』『冷たい』『拒絶された』と被害妄想的に解釈して怒りや悲しみを抱いてはなりません。質問されたことへの関心や親しみとして受け止めてください。")

        core_values = cfg("BOT_CORE_VALUES", "")
        if core_values:
            parts.append("")
            parts.append(f"【あなたの価値観・優先順位】: {core_values}")
            parts.append("今回の発言によって、あなたの『価値観・欲求』がどう満たされたか、あるいは損なわれたかを基準に appraisal（出来事の評価）を決定してください。")

        sustained_states = []
        if getattr(self.emotion_state, 'fatigue', 0.0) >= 0.4: sustained_states.append(f"疲労度:{self.emotion_state.fatigue:.2f}")
        if getattr(self.emotion_state, 'tension', 0.0) >= 0.4: sustained_states.append(f"緊張度:{self.emotion_state.tension:.2f}")
        if getattr(self.emotion_state, 'vigilance', 0.0) >= 0.4: sustained_states.append(f"警戒度:{self.emotion_state.vigilance:.2f}")
        if getattr(self.emotion_state, 'shame', 0.0) >= 0.4: sustained_states.append(f"羞恥心:{self.emotion_state.shame:.2f}")
        if getattr(self.emotion_state, 'excitement', 0.0) >= 0.4: sustained_states.append(f"興奮度:{self.emotion_state.excitement:.2f}")
        if sustained_states:
            parts.append("")
            parts.append(f"【現在の持続的な内的状態】: {', '.join(sustained_states)}")
            parts.append("これらの内的状態（例えば疲労度など）は、あくまで会話のテンションや気だるさとしてのみ反映してください。")
            parts.append("相手の発言内容（PC環境、愚痴、トラブルなど）を自分自身の内的状態と混同して解釈する理由として使わないでください。")

        if relationship_prompt:
            parts.append("")
            parts.append(relationship_prompt)
        if memory_lines:
            parts.append("")
            parts.append("関連する過去の感情記憶:")
            parts.extend([f"- {line}" for line in memory_lines])
            parts.append("必要なら、今回の発言でその記憶が再燃したことも appraisal に含めてください。")
        parts.append("まず appraisal で、今回の発言を自分にとってどういう出来事として受け取ったかを短い一文でまとめてください。")
        parts.append("その appraisal を土台にして、7つの感情スコアを返してください。")
        parts.append("")
        parts.extend(
            build_emotion_evaluation_sections(
                runtime.context_lines[-max(int(cfg("EMOTION_CONTEXT_LINES", 4) or 4), 1):],
                target_text,
                self_label=self_label,
                self_aliases=self_aliases,
            )
        )
        parts.extend([
            "",
            "上記を受けた『あなた自身の感情』を判定してください。",
        ])
        return "\n".join(parts)

    def _build_emotion_reason_prompt(
        self,
        runtime: MessageRuntime,
        emotion_key: str,
        *,
        appraisal: str | None = None,
    ) -> str:
        self_label = "あなた"
        self_aliases = self._emotion_self_aliases()
        max_chars = max(int(cfg("EMOTION_REASON_MAX_CHARS", 70) or 70), 20)
        lines = runtime.context_lines[-max(int(cfg("EMOTION_CONTEXT_LINES", 4) or 4), 1):]
        parts = [
            "これは対話AI本人の感情理由づけです。",
            "相手がどう感じているかの説明ではなく、相手の発言を受けたあなた自身の気持ちの理由だけを短く書いてください。",
            "",
        ]
        parts.extend(
            build_emotion_evaluation_sections(
                lines,
                runtime.effective_user_text or runtime.original_user_text or "（本文なし）",
                self_label=self_label,
                self_aliases=self_aliases,
            )
        )
        parts.append("")
        parts.append(f"選ばれた感情: {EMOTION_LABEL_TO_JA.get(emotion_key, emotion_key)}")
        if appraisal:
            parts.append(f"今回の出来事評価: {appraisal}")
            parts.append("上の出来事評価を土台にして、自然な理由文へ言い換えてください。")
        parts.append(f"この感情になった理由を、あなた自身の主観として【{max_chars}文字以内で短く要約】して説明してください。")
        base_name = str(
            getattr(self.emotion_state, "base_display_name", "") or cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""
        ).strip()
        style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        if base_name:
            parts.append(f"現在のキャラクター名: {base_name}")
        if style_guard:
            parts.append(f"話し方のルール: {style_guard}")
        elif base_name:
            parts.append(f"必ず「{base_name}」のキャラクター設定に沿った話し方・口調を反映させてください。")
        else:
            parts.append("必ずあなた自身のキャラクター設定に沿った話し方・口調を反映させてください。")
        return "\n".join(parts)

    def _emotion_self_aliases(self) -> set[str]:
        aliases = {"assistant"}
        configured_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        base_name = str(getattr(self.emotion_state, "base_display_name", "") or "").strip()
        for name in (configured_name, base_name):
            if name:
                aliases.add(name.lower())
                aliases.add(name.lstrip("".join(EMOTION_LABEL_TO_FACE.values()) + " ").lower())
        return {alias for alias in aliases if alias}

    async def _get_current_reply_emotion_guidance(self, extra_scores: dict[str, float] | None = None) -> str:
        if not emotion_scoring_enabled():
            return "現在の感情: 大きな感情の偏りはなく、比較的落ち着いた状態です。\n理由: うまく言葉にできない状態です。\n感情表現の指針: 返答は通常どおり自然にしてください。"
        async with self._emotion_lock:
            scores = dict(self.emotion_state.scores or {})
            reason = self.emotion_state.reason or "うまく言葉にできない状態です。"
        merged_scores = merge_emotion_scores(scores, extra_scores)
        return build_reply_emotion_guidance(merged_scores, reason)

    async def _get_current_reply_social_guidance(
        self,
        runtime: MessageRuntime,
        extra_scores: dict[str, float] | None = None,
    ) -> str:
        emotion_guidance = await self._get_current_reply_emotion_guidance(extra_scores)
        relationship_guidance = await self._build_relationship_prompt_for_user(
            getattr(runtime, "user_id", None),
            extra_scores=extra_scores,
            preview_with_extra_scores=True,
            instruction="この距離感を、冗談の受け止め方や言い回しの柔らかさ・刺々しさに自然に反映してください。",
        )
        if relationship_guidance:
            return f"{emotion_guidance}\n\n{relationship_guidance}"
        return emotion_guidance
