"""
[AI Agent Summary]
このファイルは、ユーザーからのメッセージを元に感情スコアを計算し、内的状態（emotion_state）や関係値ベクトルを更新するメインロジックを担当する Mixin です。
This mixin handles the main logic of scoring emotions based on user messages and updating internal states (emotion_state) and relationship vectors.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, TYPE_CHECKING

import discord

from ...common.config_helpers import cfg, cfg_float, cfg_int
from ...common.emotion_helpers import (
    EMOTION_KEYS,
    apply_emotion_persona_weights,
    clamp_score,
    emotion_scoring_enabled,
    fallback_reason_text,
    looks_like_bad_reason_text,
    merge_emotion_scores,
    normalize_llm_emotion_scores,
    normalize_reason_text,
)
from ...common.ollama_helpers import call_ollama_json, truncate_text

from ..ollama_chat_texts import (
    EMOTION_REASONER_SYSTEM_PROMPT,
    build_emotion_scorer_system_prompt,
)
from ..ollama_chat_types import MessageRuntime, UserRelationship

log = logging.getLogger("ollama_bot.ollama_chat")


from .models_logic import (
    EMOTION_REASON_SCHEMA,
    _emotion_scorer_model_name,
    _emotion_score_schema,
    _emotion_score_uses_integer_scale,
    _normalize_emotion_scores_from_model,
    _normalize_scored_reaction,
    _resolve_emotion_blend_params,
)
from .utils import (
    _has_meaningful_emotion_signal,
    _normalize_appraisal_text,
    _looks_like_bad_appraisal_text,
    _fallback_appraisal_text,
    _peak_emotion_key,
    _reason_from_appraisal,
    _blend_emotion_state_score,
    is_neutral_or_decayed_reason,
    is_neutral_or_decayed_appraisal,
)
from .relationships_logic import (
    _relationship_delta_from_scores,
    _apply_relationship_delta,
    _copy_relationship,
    _fallback_user_thought,
)
from .relationships_thought_logic import (
    generate_user_relationship_thought,
    looks_like_placeholder_thought,
    should_update_user_thought,
)
from .heuristics import _infer_emotion_scores_without_llm

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionScoringBase = OllamaChatProtocol
else:
    _EmotionScoringBase = object


class OllamaChatEmotionScoringMixin(_EmotionScoringBase):
    async def _update_user_relationship(
        self,
        user_id: int | None,
        scores: dict[str, float] | None,
        *,
        appraisal: str | None = None,
    ) -> UserRelationship | None:
        if user_id is None:
            return None
        self._ensure_relationship_state()
        affinity_delta, trust_delta, affection_delta, hatred_delta = _relationship_delta_from_scores(scores)
        min_delta = max(float(cfg("RELATIONSHIP_MIN_DELTA", 0.001) or 0.001), 0.0)
        has_meaningful_delta = (
            abs(affinity_delta) >= min_delta
            or abs(trust_delta) >= min_delta
            or abs(affection_delta) >= min_delta
            or abs(hatred_delta) >= min_delta
        )
        async with self._relationship_lock:
            relationship = self.user_relationships.setdefault(int(user_id), UserRelationship())
            if has_meaningful_delta:
                updated = _apply_relationship_delta(relationship, scores)
            else:
                updated = relationship
            updated.last_interaction_ts = time.time()
            updated.interaction_count = int(getattr(updated, "interaction_count", 0) or 0) + 1
            # 一言（所感）が未設定またはプレースホルダーの場合のみフォールバック定型文を設定（既存の正常なLLM生成コメントは保持）
            if not str(updated.thought or "").strip() or looks_like_placeholder_thought(updated.thought):
                updated.thought = _fallback_user_thought(updated)
            snapshot = _copy_relationship(updated)
        return snapshot

    async def _update_user_thought_async(self, user_id: int, *, user_name: str = "") -> None:
        """非同期でユーザーの一言所感をLLMを用いて分析・生成し、更新する。"""
        self._ensure_relationship_state()
        store = getattr(self, "memory_store", None)
        logs: list[dict[str, Any]] = []
        if store is not None:
            try:
                get_logs_fn = getattr(store, "get_recent_user_relationship_logs", None)
                if callable(get_logs_fn):
                    logs = await get_logs_fn(
                        persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
                        user_id=int(user_id),
                        limit=10,
                    )
            except Exception as e:
                log.warning("failed to fetch user relationship logs for thought generation: %r", e)

        async with self._relationship_lock:
            current_rel = self.user_relationships.get(int(user_id))
            if current_rel is None:
                return
            rel_copy = _copy_relationship(current_rel)

        base_name = str(
            getattr(self.emotion_state, "base_display_name", "") or cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "AI"
        ).strip() or "AI"

        new_thought = await generate_user_relationship_thought(
            rel_copy,
            logs,
            bot_name=base_name,
            user_name=user_name or f"ユーザー_{user_id}",
        )
        if new_thought:
            async with self._relationship_lock:
                rel = self.user_relationships.get(int(user_id))
                if rel is not None:
                    rel.thought = new_thought
                    rel.last_thought_generated_ts = time.time()
                    rel.interaction_count = 0
            await self._save_user_relationships()
            log.info("[所感更新完了] ユーザー %s の一言所感を更新・保存しました: %s", user_id, new_thought)
        else:
            # 生成失敗またはプレースホルダー棄却時：既存のthoughtがプレースホルダーならフォールバックで上書き修復
            need_save = False
            repaired_thought = ""
            async with self._relationship_lock:
                rel = self.user_relationships.get(int(user_id))
                if rel is not None and looks_like_placeholder_thought(rel.thought):
                    rel.thought = _fallback_user_thought(rel, base_name=base_name)
                    repaired_thought = rel.thought
                    need_save = True
            if need_save:
                await self._save_user_relationships()
                log.info("[所感フォールバック修復] ユーザー %s のプレースホルダー所感を定型文で修復・保存しました: %s", user_id, repaired_thought)


    def _pick_dominant_emotion(self, scores: dict[str, float]) -> str:
        if not scores:
            return "neutral"
        best_key = max(scores, key=lambda k: scores.get(k, 0.0))
        best_score = scores.get(best_key, 0.0)
        threshold = float(cfg("EMOTION_MIN_ACTIVE_SCORE", 0.20) or 0.20)
        return best_key if best_score >= threshold else "neutral"

    def _emotion_scorer_model_for_release(self) -> str:
        return _emotion_scorer_model_name()

    def _heuristic_emotion_with_appraisal_for_text(self, text: str) -> tuple[dict[str, float], str, str]:
        heuristic_scores = _infer_emotion_scores_without_llm(text)
        scores = apply_emotion_persona_weights(heuristic_scores)
        appraisal = _fallback_appraisal_text(_peak_emotion_key(scores))
        return scores, appraisal, "NONE"

    async def _score_emotion_with_appraisal(self, runtime: MessageRuntime) -> tuple[dict[str, float], str, str]:
        current_scores, current_dominant, current_reason = await self._get_emotion_snapshot()
        async with self._emotion_lock:
            current_appraisal = str(getattr(self.emotion_state, "appraisal", "") or "").strip()
        relationship_prompt = await self._build_relationship_prompt_for_user(
            getattr(runtime, "user_id", None),
            instruction="同じ言葉でも、この距離感を踏まえて appraisal を決めてください。",
        )
        memory_lines = await self._get_emotion_memory_lines(
            runtime,
            current_scores=current_scores,
        )
        try:
            result = await call_ollama_json(
                self._build_emotion_prompt(
                    runtime,
                    current_scores=current_scores,
                    current_dominant=current_dominant,
                    current_appraisal=current_appraisal,
                    current_reason=current_reason,
                    memory_lines=memory_lines,
                    relationship_prompt=relationship_prompt,
                ),
                system_prompt=build_emotion_scorer_system_prompt(integer_scale=_emotion_score_uses_integer_scale()),
                think=False,
                model=_emotion_scorer_model_name(),
                schema=_emotion_score_schema(),
                timeout_sec=float(cfg("EMOTION_SCORE_TIMEOUT_SEC", cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60)) or 60),
                retries=max(int(cfg("EMOTION_SCORE_RETRIES", cfg("OLLAMA_JSON_RETRIES", 1)) or 1), 0),
                temperature=float(cfg("EMOTION_SCORE_TEMPERATURE", 0.1) or 0.1),
                num_predict=max(int(cfg("EMOTION_SCORE_NUM_PREDICT", 192) or 192), 96),
            )
        except Exception as e:
            text = runtime.effective_user_text or runtime.original_user_text or ""
            scores, appraisal, reaction = self._heuristic_emotion_with_appraisal_for_text(text)
            log.warning(
                "emotion scorer failed or returned invalid schema; using heuristic fallback text=%r err=%r",
                truncate_text(text, 120),
                e,
            )
            return scores, appraisal, reaction
        raw_scores = _normalize_emotion_scores_from_model(result)
        appraisal = _normalize_appraisal_text(
            str(result.get("appraisal") or "").strip(),
            max(int(cfg("EMOTION_APPRAISAL_MAX_CHARS", 80) or 80), 20),
        )
        appraisal_is_usable = not _looks_like_bad_appraisal_text(appraisal)
        if not _has_meaningful_emotion_signal(raw_scores) and not appraisal_is_usable:
            heuristic_scores = _infer_emotion_scores_without_llm(
                runtime.effective_user_text or runtime.original_user_text or ""
            )
            if _has_meaningful_emotion_signal(heuristic_scores):
                log.warning(
                    "emotion scorer returned flat scores and no usable appraisal; using heuristic fallback text=%r scores=%r",
                    truncate_text(runtime.effective_user_text or runtime.original_user_text or "", 120),
                    heuristic_scores,
                )
                raw_scores = heuristic_scores
        scores = apply_emotion_persona_weights(raw_scores)
        if not appraisal_is_usable:
            appraisal = _fallback_appraisal_text(_peak_emotion_key(scores))
        reaction = _normalize_scored_reaction(result.get("reaction"))
        return scores, appraisal, reaction

    async def _score_emotion(self, runtime: MessageRuntime) -> dict[str, float]:
        scores, _, _ = await self._score_emotion_with_appraisal(runtime)
        return scores

    async def _reason_emotion(self, runtime: MessageRuntime, emotion_key: str, *, appraisal: str | None = None) -> str:
        max_chars = max(int(cfg("EMOTION_REASON_MAX_CHARS", 70) or 70), 20)
        if emotion_key == "neutral":
            if appraisal:
                return _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars)
            return "平常状態"
        try:
            result = await call_ollama_json(
                self._build_emotion_reason_prompt(runtime, emotion_key, appraisal=appraisal),
                system_prompt=EMOTION_REASONER_SYSTEM_PROMPT,
                think=False,
                model=_emotion_scorer_model_name(),
                schema=EMOTION_REASON_SCHEMA,
                timeout_sec=max(cfg_float("EMOTION_REASON_TIMEOUT_SEC", 20.0), 5.0),
                retries=max(cfg_int("EMOTION_REASON_RETRIES", 1), 0),
                num_predict=max(cfg_int("EMOTION_REASON_NUM_PREDICT", 96), 32),
            )
            raw_reason = str(result.get("reason") or "").strip()
            reason = normalize_reason_text(raw_reason, max_chars)
            if looks_like_bad_reason_text(reason):
                log.warning("emotion reason rejected as unnatural/non-japanese: raw=%r normalized=%r emotion=%s", raw_reason, reason, emotion_key)
                return _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars)
            return reason
        except Exception as e:
            log.warning("emotion reason failed: %r", e)
            return _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars)

    async def _update_emotion_state(
        self,
        runtime: MessageRuntime,
        incoming_scores: dict[str, float] | None = None,
        incoming_appraisal: str | None = None,
        incoming_reaction: str | None = None,
    ) -> str:
        if not emotion_scoring_enabled():
            return "NONE"
        max_appraisal_chars = max(int(cfg("EMOTION_APPRAISAL_MAX_CHARS", 80) or 80), 20)
        reaction_result = "NONE"
        if incoming_scores is None:
            try:
                incoming, appraisal, reaction_result = await self._score_emotion_with_appraisal(runtime)
            except Exception as e:
                log.warning("emotion scoring failed: %r", e)
                return "NONE"
        else:
            incoming = {k: clamp_score(v) for k, v in incoming_scores.items()}
            appraisal = _normalize_appraisal_text(str(incoming_appraisal or "").strip(), max_appraisal_chars)
            reaction_result = incoming_reaction or "NONE"
        decay, blend, used_low_signal_carry = _resolve_emotion_blend_params(incoming)
        async with self._emotion_lock:
            previous_dominant = str(self.emotion_state.dominant_emotion or "neutral")
            previous_appraisal = str(getattr(self.emotion_state, "appraisal", "") or "").strip()
            previous_reason = str(self.emotion_state.reason or "").strip()
            for key in EMOTION_KEYS:
                current = self.emotion_state.scores.get(key, 0.0)
                updated = _blend_emotion_state_score(current, incoming.get(key, 0.0), decay=decay, blend=blend)
                self.emotion_state.scores[key] = clamp_score(updated)
            dominant = self._pick_dominant_emotion(self.emotion_state.scores)
            self.emotion_state.dominant_emotion = dominant

            joy = incoming.get("joy", 0.0)
            surprise = incoming.get("surprise", 0.0)
            fear = incoming.get("fear", 0.0)
            anger = incoming.get("anger", 0.0)
            anticipation = incoming.get("anticipation", 0.0)
            sadness = incoming.get("sadness", 0.0)
            disgust = incoming.get("disgust", 0.0)

            self.emotion_state.fatigue = clamp_score(getattr(self.emotion_state, 'fatigue', 0.0) + 0.02)
            self.emotion_state.tension = _blend_emotion_state_score(getattr(self.emotion_state, 'tension', 0.0), max(fear, anger, anticipation), decay=0.90, blend=0.20)
            self.emotion_state.excitement = _blend_emotion_state_score(getattr(self.emotion_state, 'excitement', 0.0), max(joy, surprise), decay=0.85, blend=0.25)
            self.emotion_state.shame = _blend_emotion_state_score(getattr(self.emotion_state, 'shame', 0.0), sadness if sadness > 0.4 else 0.0, decay=0.85, blend=0.20)
            self.emotion_state.vigilance = _blend_emotion_state_score(getattr(self.emotion_state, 'vigilance', 0.0), fear if fear > 0.0 else (disgust if disgust > 0.4 else 0.0), decay=0.90, blend=0.20)

            self.emotion_state.last_trigger_text = truncate_text(runtime.original_user_text, 120) or ""
        fallback_appraisal_key = dominant if dominant != "neutral" else _peak_emotion_key(incoming)
        if used_low_signal_carry and dominant == previous_dominant:
            # 主要感情がアクティブ（非neutral）なのに前回の理由・所感が平常・減衰系の場合は引き継がない
            can_reuse_reason = bool(previous_reason) and not (dominant != "neutral" and is_neutral_or_decayed_reason(previous_reason))
            can_reuse_appraisal = bool(previous_appraisal) and not (dominant != "neutral" and is_neutral_or_decayed_appraisal(previous_appraisal))

            if can_reuse_appraisal:
                appraisal = previous_appraisal
            elif _looks_like_bad_appraisal_text(appraisal) or is_neutral_or_decayed_appraisal(appraisal):
                appraisal = _fallback_appraisal_text(fallback_appraisal_key)

            if can_reuse_reason:
                reason = previous_reason
            else:
                reason = await self._reason_emotion(runtime, dominant, appraisal=appraisal)
        else:
            if _looks_like_bad_appraisal_text(appraisal):
                appraisal = _fallback_appraisal_text(fallback_appraisal_key)
            reason = await self._reason_emotion(runtime, dominant, appraisal=appraisal)

        # 多重防御: 主要感情がアクティブ（非neutral）なのに理由が平常・減衰系になっている場合は感情に即した理由に修復
        if dominant != "neutral" and is_neutral_or_decayed_reason(reason):
            reason = fallback_reason_text(dominant)

        async with self._emotion_lock:
            self.emotion_state.appraisal = appraisal
            self.emotion_state.reason = reason
            mood, mood_reason = self._derive_mood_from_state()
            self.emotion_state.mood = mood
            self.emotion_state.mood_reason = mood_reason
        relationship_snapshot = await self._update_user_relationship(
            getattr(runtime, "user_id", None),
            incoming,
            appraisal=appraisal,
        )
        if relationship_snapshot is not None:
            await self._save_user_relationships()

        # 対話ログの永続化（MemoryStore）
        store = getattr(self, "memory_store", None)
        user_id_val = getattr(runtime, "user_id", None)
        if store is not None and user_id_val is not None:
            try:
                record_fn = getattr(store, "record_user_relationship_log", None)
                if callable(record_fn):
                    user_text = (runtime.effective_user_text or runtime.original_user_text or "").strip()
                    user_name = getattr(runtime, "author_name", "") or ""
                    await record_fn(
                        persona=str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default"),
                        guild_id=getattr(runtime, "guild_id", None),
                        channel_id=getattr(runtime, "cid", None),
                        user_id=int(user_id_val),
                        user_name=user_name,
                        user_text=user_text,
                        bot_appraisal=appraisal,
                        dominant_emotion=dominant,
                        emotion_scores=incoming,
                        affinity=float(getattr(relationship_snapshot, "affinity", 0.0) or 0.0),
                        trust=float(getattr(relationship_snapshot, "trust", 0.0) or 0.0),
                        affection=float(getattr(relationship_snapshot, "affection", 0.0) or 0.0),
                        hatred=float(getattr(relationship_snapshot, "hatred", 0.0) or 0.0),
                    )
            except Exception as e:
                log.warning("failed to record user relationship log: %r", e)

        # 一言コメント（thought）の更新チェックとバックグラウンド生成トリガー
        if relationship_snapshot is not None and user_id_val is not None:
            if should_update_user_thought(relationship_snapshot, current_ts=time.time()):
                user_name = getattr(runtime, "author_name", "") or ""
                asyncio.create_task(self._update_user_thought_async(int(user_id_val), user_name=user_name))

        trust = float(getattr(relationship_snapshot, "trust", 0.0)) if relationship_snapshot else 0.0
        affinity = float(getattr(relationship_snapshot, "affinity", 0.0)) if relationship_snapshot else 0.0
        affection = float(getattr(relationship_snapshot, "affection", 0.0)) if relationship_snapshot else 0.0
        hatred = float(getattr(relationship_snapshot, "hatred", 0.0)) if relationship_snapshot else 0.0

        async with self._emotion_lock:
            fatigue = getattr(self.emotion_state, 'fatigue', 0.0)
            tension = getattr(self.emotion_state, 'tension', 0.0)
            dominant = self.emotion_state.dominant_emotion

            policy = "普段通り"
            if fatigue > 0.8:
                policy = "塩対応（最小限の相槌のみで早々に会話を切り上げる）"
            elif hatred >= 5.0:
                policy = "完全拒絶・冷徹（強い憎しみを持って冷たく突き放す）"
            elif hatred >= 2.5 and affection < 2.0:
                policy = "敵愾心・皮肉（冷淡かつ棘のある態度で接する）"
            elif affection >= 2.5 and hatred >= 2.5:
                policy = "愛憎葛藤（素直になれず反発しつつも気にかけてしまう）"
            elif tension > 0.75 and trust <= 0:
                policy = "警戒・事務的（無難に感情を交えず返す）"
            elif dominant in ("anger", "disgust") and trust < 0:
                policy = "突き放す（不快感を示し、距離を置く）"
            elif dominant == "sadness" and trust > 0.2:
                policy = "慰める・寄り添う（相手の感情に同調する）"
            elif dominant == "joy" and (affinity > 0.4 or affection > 0.5):
                policy = "茶化す・盛り上がる（冗談交じりで親しげに乗っかる）"
            elif affection >= 4.0 and hatred <= 0.0:
                policy = "親愛・好意（好意を隠さず優しく素直に接する）"

            self.emotion_state.action_policy = policy

        self._save_emotion_state()
        try:
            sync_presence = getattr(self, "_sync_emotion_presence", None)
            if callable(sync_presence):
                asyncio.create_task(sync_presence())
        except Exception as e:
            log.warning("presence sync scheduling failed: %r", e)
        return reaction_result
