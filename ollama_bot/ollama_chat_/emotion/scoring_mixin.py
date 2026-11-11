"""
[AI Agent Summary]
このファイルは、ユーザーメッセージおよびBot返答テキストを元に感情スコアを計算し、内的状態（emotion_state）や関係値ベクトルを更新するメインロジックを担当する Mixin です。
返信確定直後にゼロ遅延でヒューリスティック感情状態を先行反映する Phase 1（_apply_heuristic_emotion_immediate）と、非同期でLLM詳細アプレイザルを実行する Phase 2（_update_emotion_state）の二段階更新（Two-Phase Emotion Update）を統括します。
This mixin handles emotion scoring based on user messages and bot replies, updating internal states (emotion_state) and relationship vectors, featuring a Two-Phase Emotion Update architecture.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import logging
import time
from typing import Any, TYPE_CHECKING

import discord

from ...common.config_helpers import cfg, cfg_float, cfg_int, cfg_persona_namespace
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
from harness.agent.observation.emotion_evaluator import EmotionObservationEvaluator

log = logging.getLogger("ollama_bot.ollama_chat")
_emotion_evaluator = EmotionObservationEvaluator()



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
    is_negative_mitigated_appraisal,
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

    def _ensure_thought_generation_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_thought_generation_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._thought_generation_lock = lock
        return lock

    async def _update_user_thought_async(
        self,
        user_id: int,
        user_name: str = "",
        *,
        initial_delay_sec: float = 0.0,
    ) -> None:
        """非同期でユーザーの一言所感をLLMを用いて分析・生成し、更新する。
        先行する返信後タスク（長期記憶抽出等）との排他制御・直列化を行い、ローカル27Bモデル等の過密競合を防止する。
        """
        # 初期ディレイ（返信直後の記憶抽出タスク先行起動・待機用）
        if initial_delay_sec > 0.0:
            log.debug(
                "[所感生成初期遅延] 返信後タスク競合防止のため %.1f 秒待機します (user_id=%s)",
                initial_delay_sec,
                user_id,
            )
            await asyncio.sleep(initial_delay_sec)

        # 先行するバックグラウンドLLMタスク（長期記憶抽出タスク等）の完了を待機
        current_task = asyncio.current_task()
        prev_bg_task = getattr(self, "_current_background_llm_task", None)
        if (
            prev_bg_task is not None
            and isinstance(prev_bg_task, asyncio.Task)
            and not prev_bg_task.done()
            and prev_bg_task is not current_task
        ):
            try:
                task_name = getattr(prev_bg_task, "get_name", lambda: str(prev_bg_task))()
                log.info("[所感生成待機] 先行するバックグラウンドLLMタスクの完了を待機します: %s", task_name)
                await asyncio.shield(prev_bg_task)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("先行バックグラウンドLLMタスク待機終了 (例外): %r", e)

        self._ensure_relationship_state()
        store = getattr(self, "memory_store", None)
        logs: list[dict[str, Any]] = []
        if store is not None:
            try:
                get_logs_fn = getattr(store, "get_recent_user_relationship_logs", None)
                if callable(get_logs_fn):
                    logs = await get_logs_fn(
                        persona=cfg_persona_namespace(),
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

        resolved_name = (user_name or "").strip()
        if not resolved_name or resolved_name.startswith("ユーザー_"):
            guilds = getattr(self.bot, "guilds", []) or []
            for g in guilds:
                m = g.get_member(int(user_id))
                if m is not None:
                    m_name = (getattr(m, "display_name", "") or getattr(m, "name", "") or "").strip()
                    if m_name and not m_name.startswith("ユーザー_"):
                        resolved_name = m_name
                        break

            if (not resolved_name or resolved_name.startswith("ユーザー_")) and store is not None:
                resolve_fn = getattr(store, "resolve_user_display_name", None)
                if callable(resolve_fn):
                    try:
                        res = resolve_fn(int(user_id))
                        if asyncio.iscoroutine(res):
                            res = await res
                        if res and str(res).strip() and not str(res).strip().startswith("ユーザー_"):
                            resolved_name = str(res).strip()
                    except Exception as e:
                        log.debug("resolve_user_display_name failed for %s: %r", user_id, e)

                if not resolved_name or resolved_name.startswith("ユーザー_"):
                    get_profile_fn = getattr(store, "get_user_profile", None)
                    if callable(get_profile_fn):
                        try:
                            prof = get_profile_fn(int(user_id))
                            if asyncio.iscoroutine(prof):
                                prof = await prof
                            if isinstance(prof, dict):
                                prof_name = str(prof.get("display_name") or prof.get("user_name") or "").strip()
                                if prof_name and not prof_name.startswith("ユーザー_"):
                                    resolved_name = prof_name
                        except Exception as e:
                            log.debug("get_user_profile failed for %s: %r", user_id, e)

        final_user_name = resolved_name or f"ユーザー_{user_id}"

        base_name = str(
            getattr(self.emotion_state, "base_display_name", "") or cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "AI"
        ).strip() or "AI"

        yield_fn = getattr(self, "_yield_to_user_replies", None)

        thought_lock = self._ensure_thought_generation_lock()
        bg_lock = getattr(self, "_bg_llm_lock", None)

        async with thought_lock:
            async with AsyncExitStack() as stack:
                if bg_lock is not None:
                    await stack.enter_async_context(bg_lock)

                if callable(yield_fn):
                    await yield_fn()

                old_bg_task = getattr(self, "_current_background_llm_task", None)
                self._current_background_llm_task = current_task
                try:
                    new_thought = await generate_user_relationship_thought(
                        rel_copy,
                        logs,
                        bot_name=base_name,
                        user_name=final_user_name,
                    )
                finally:
                    if getattr(self, "_current_background_llm_task", None) is current_task:
                        self._current_background_llm_task = (
                            old_bg_task if (old_bg_task is not None and not old_bg_task.done()) else None
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


    def _pick_dominant_emotion(
        self,
        scores: dict[str, float],
        incoming_scores: dict[str, float] | None = None,
        appraisal: str | None = None,
    ) -> str:
        """感情スコアおよび出来事評価から支配的感情を観察・判定する（harness.agent.observation.emotion_evaluatorへ委譲）。"""
        threshold = float(cfg("EMOTION_MIN_ACTIVE_SCORE", 0.20) or 0.20)
        return _emotion_evaluator.pick_dominant_emotion(
            scores,
            incoming_scores=incoming_scores,
            appraisal=appraisal,
            threshold=threshold,
        )

    def _emotion_scorer_model_for_release(self) -> str:
        return _emotion_scorer_model_name()

    def _heuristic_emotion_with_appraisal_for_text(
        self,
        text: str,
        assistant_text: str = "",
        *,
        is_reply_to_other_user: bool = False,
    ) -> tuple[dict[str, float], str, str]:
        heuristic_scores = _infer_emotion_scores_without_llm(text, assistant_text=assistant_text)
        scores = apply_emotion_persona_weights(heuristic_scores)
        appraisal = _fallback_appraisal_text(_peak_emotion_key(scores), is_reply_to_other_user=is_reply_to_other_user)
        return scores, appraisal, "NONE"

    def _recompute_composite_indices(
        self,
        scores: dict[str, float],
        *,
        fatigue_delta: float = 0.01,
    ) -> None:
        """感情スコアから内的複合指標（fatigue, tension, excitement, shame, vigilance）を再計算する。"""
        joy = float(scores.get("joy", 0.0))
        surprise = float(scores.get("surprise", 0.0))
        fear = float(scores.get("fear", 0.0))
        anger = float(scores.get("anger", 0.0))
        anticipation = float(scores.get("anticipation", 0.0))
        sadness = float(scores.get("sadness", 0.0))
        disgust = float(scores.get("disgust", 0.0))

        # 疲労度
        self.emotion_state.fatigue = clamp_score(getattr(self.emotion_state, 'fatigue', 0.0) + fatigue_delta)

        # 緊張・警戒度（tension）
        # 恐怖・怒り・嫌悪を主軸とし、anticipation は恐怖・怒りが一定以上（>= 0.25）ある場合の増幅に留める
        neg_peaks = max(fear, anger, disgust * 0.8)
        if max(fear, anger) >= 0.25:
            incoming_tension = max(neg_peaks, anticipation * 0.8)
        else:
            incoming_tension = neg_peaks

        if joy >= 0.4:
            incoming_tension = min(incoming_tension, 0.45)

        current_tension = float(getattr(self.emotion_state, 'tension', 0.0) or 0.0)
        new_tension = _blend_emotion_state_score(current_tension, incoming_tension, decay=0.90, blend=0.20)
        if joy >= 0.4:
            new_tension = min(new_tension, 0.49)
        self.emotion_state.tension = clamp_score(new_tension)

        # 興奮度、羞恥心、警戒度
        self.emotion_state.excitement = _blend_emotion_state_score(
            getattr(self.emotion_state, 'excitement', 0.0),
            max(joy, surprise),
            decay=0.85,
            blend=0.25,
        )
        self.emotion_state.shame = _blend_emotion_state_score(
            getattr(self.emotion_state, 'shame', 0.0),
            sadness if sadness > 0.4 else 0.0,
            decay=0.85,
            blend=0.20,
        )
        self.emotion_state.vigilance = _blend_emotion_state_score(
            getattr(self.emotion_state, 'vigilance', 0.0),
            fear if fear > 0.0 else (disgust if disgust > 0.4 else 0.0),
            decay=0.90,
            blend=0.20,
        )

        dominant = self.emotion_state.dominant_emotion or "neutral"
        mood, mood_reason = self._derive_mood_from_state()
        self.emotion_state.mood = mood
        self.emotion_state.mood_reason = mood_reason

        log.info(
            "[emotion] Composite updated: dominant=%s mood=%s tension=%.2f (fear=%.2f, anger=%.2f, joy=%.2f, anticipation=%.2f)",
            dominant,
            mood,
            self.emotion_state.tension,
            fear,
            anger,
            joy,
            anticipation,
        )

    def _derive_action_policy(
        self,
        *,
        fatigue: float,
        tension: float,
        dominant: str,
        trust: float,
        affinity: float,
        affection: float,
        hatred: float,
        scores: dict[str, float] | None = None,
    ) -> str:
        scores = scores or (self.emotion_state.scores or {})
        joy = float(scores.get("joy", 0.0))
        fear = float(scores.get("fear", 0.0))
        anger = float(scores.get("anger", 0.0))
        if joy >= 0.5 and max(fear, anger) < 0.3:
            return "engaged"
        policy_eval = _emotion_evaluator.evaluate_action_policy(
            fatigue=fatigue,
            tension=tension,
            dominant=dominant,
            trust=trust,
            affinity=affinity,
            affection=affection,
            hatred=hatred,
        )
        policy = policy_eval.policy
        if joy >= 0.5 and max(fear, anger) < 0.3 and ("警戒" in policy or "事務的" in policy):
            return "engaged"
        return policy

    async def _apply_heuristic_emotion_immediate(
        self,
        runtime: MessageRuntime,
        *,
        assistant_text: str = "",
    ) -> tuple[dict[str, float], str, str]:
        """返信確定直後などにゼロ遅延でヒューリスティック感情状態・Presenceを先行更新する（Two-Phase Emotion Update Phase 1）。"""
        if not emotion_scoring_enabled():
            return {}, "平常状態", "NONE"

        msg = getattr(runtime, "message", None)
        if msg is not None:
            bot_obj = getattr(self, "bot", None)
            bot_user_id = getattr(getattr(bot_obj, "user", None), "id", None)
            from ...common.bot_identity import is_message_addressed_to_other_bot
            if is_message_addressed_to_other_bot(msg, bot_user_id=bot_user_id, bot=bot_obj):
                return {}, "平常状態", "NONE"

        user_text = runtime.effective_user_text or runtime.original_user_text or ""
        eff_asst = assistant_text or getattr(runtime, "assistant_text", "") or ""
        working_ctx = getattr(runtime, "working_context", None) or {}
        is_reply_to_other = bool(
            getattr(runtime, "is_reply_to_other_user", False)
            or working_ctx.get("is_reply_to_other_user")
        )
        scores, appraisal, reaction = self._heuristic_emotion_with_appraisal_for_text(
            user_text,
            assistant_text=eff_asst,
            is_reply_to_other_user=is_reply_to_other,
        )

        decay, blend, _ = _resolve_emotion_blend_params(scores)
        async with self._emotion_lock:
            for key in EMOTION_KEYS:
                current = self.emotion_state.scores.get(key, 0.0)
                updated = _blend_emotion_state_score(current, scores.get(key, 0.0), decay=decay, blend=blend)
                self.emotion_state.scores[key] = clamp_score(updated)

            dominant = self._pick_dominant_emotion(
                self.emotion_state.scores,
                incoming_scores=scores,
                appraisal=appraisal,
            )
            self.emotion_state.dominant_emotion = dominant
            self.emotion_state.appraisal = appraisal
            self.emotion_state.reason = (
                _emotion_evaluator.fallback_reason_text(dominant, is_reply_to_other_user=is_reply_to_other)
                if dominant != "neutral"
                else "平常状態"
            )

            self.emotion_state.last_trigger_text = truncate_text(runtime.original_user_text, 120) or ""
            self.emotion_state.last_trigger_user_id = getattr(runtime, "user_id", None)
            self._recompute_composite_indices(scores, fatigue_delta=0.01)

        self._save_emotion_state()
        try:
            sync_presence = getattr(self, "_sync_emotion_presence", None)
            if callable(sync_presence):
                asyncio.create_task(sync_presence())
        except Exception as e:
            log.warning("immediate presence sync scheduling failed: %r", e)

        log.info(
            "TRACE emotion: immediate heuristic state applied dominant=%s peak=%s reason=%r",
            dominant,
            _peak_emotion_key(scores),
            self.emotion_state.reason,
        )
        return scores, appraisal, reaction

    async def _score_emotion_with_appraisal(self, runtime: MessageRuntime) -> tuple[dict[str, float], str, str]:
        current_scores, current_dominant, current_reason = await self._get_emotion_snapshot()
        async with self._emotion_lock:
            current_appraisal = str(getattr(self.emotion_state, "appraisal", "") or "").strip()
        target_user_name = (
            getattr(runtime, "author_display_name", "")
            or getattr(runtime, "author_name", "")
            or ""
        )
        relationship_prompt = await self._build_relationship_prompt_for_user(
            getattr(runtime, "user_id", None),
            instruction="同じ言葉でも、この距離感を踏まえて appraisal を決めてください。",
            target_user_name=target_user_name,
        )
        memory_lines = await self._get_emotion_memory_lines(
            runtime,
            current_scores=current_scores,
        )
        if memory_lines:
            memory_lines = [
                m for m in memory_lines
                if not any(
                    kw in m.lower()
                    for kw in (
                        "fact_check", "factcheck", "research", "gemini api",
                        "以前ファクトチェックした話題:", "ファクトチェック",
                        "web検索", "調査結果", "検索結果", "mcp"
                    )
                )
            ]
        configured_score_timeout = float(cfg("EMOTION_SCORE_TIMEOUT_SEC", 25.0) or 25.0)
        score_timeout = min(configured_score_timeout, 25.0) if configured_score_timeout > 0 else 25.0
        score_retries = 0
        working_ctx = getattr(runtime, "working_context", None) or {}
        is_reply_to_other = bool(
            getattr(runtime, "is_reply_to_other_user", False)
            or working_ctx.get("is_reply_to_other_user")
        )
        try:
            yield_fn = getattr(self, "_yield_to_user_replies", None)
            if callable(yield_fn):
                await yield_fn()
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
                timeout_sec=score_timeout,
                retries=score_retries,
                temperature=float(cfg("EMOTION_SCORE_TEMPERATURE", 0.1) or 0.1),
                num_predict=max(int(cfg("EMOTION_SCORE_NUM_PREDICT", 192) or 192), 96),
            )
        except asyncio.CancelledError:
            text = runtime.effective_user_text or runtime.original_user_text or ""
            asst_text = getattr(runtime, "assistant_text", "") or ""
            log.info(
                "emotion scorer cancelled (semaphore wait or timeout); re-raising CancelledError to abort further emotion LLM steps text=%r asst=%r",
                truncate_text(text, 120),
                truncate_text(asst_text, 60),
            )
            raise
        except Exception as e:
            text = runtime.effective_user_text or runtime.original_user_text or ""
            asst_text = getattr(runtime, "assistant_text", "") or ""
            scores, appraisal, reaction = self._heuristic_emotion_with_appraisal_for_text(
                text,
                assistant_text=asst_text,
                is_reply_to_other_user=is_reply_to_other,
            )
            log.warning(
                "emotion scorer failed; using heuristic fallback text=%r asst=%r err=%r",
                truncate_text(text, 120),
                truncate_text(asst_text, 60),
                e,
            )
            return scores, appraisal, reaction
        raw_scores = _normalize_emotion_scores_from_model(result)
        appraisal = _normalize_appraisal_text(
            str(result.get("appraisal") or "").strip(),
            max(int(cfg("EMOTION_APPRAISAL_MAX_CHARS", 80) or 80), 20),
        )
        appraisal_is_usable = not _looks_like_bad_appraisal_text(appraisal, is_reply_to_other_user=is_reply_to_other)
        if not _has_meaningful_emotion_signal(raw_scores) and not appraisal_is_usable:
            asst_text = getattr(runtime, "assistant_text", "") or ""
            heuristic_scores = _infer_emotion_scores_without_llm(
                runtime.effective_user_text or runtime.original_user_text or "",
                assistant_text=asst_text,
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
            appraisal = _fallback_appraisal_text(_peak_emotion_key(scores), is_reply_to_other_user=is_reply_to_other)
        reaction = _normalize_scored_reaction(result.get("reaction"))

        # 言葉遊び・クイズに対する被害妄想（愚痴の無視・軽蔑など）による怒り・嫌悪の暴走を防止（harness.agent.observation.emotion_evaluatorへ委譲）
        user_raw_text = runtime.effective_user_text or runtime.original_user_text or ""
        scores, appraisal, reaction, _ = _emotion_evaluator.mitigate_wordplay_paranoid_anger(
            user_raw_text,
            appraisal,
            scores,
            reaction,
        )
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
        configured_reason_timeout = cfg_float("EMOTION_REASON_TIMEOUT_SEC", 15.0)
        reason_timeout = min(configured_reason_timeout, 15.0) if configured_reason_timeout > 0 else 15.0
        reason_retries = max(cfg_int("EMOTION_REASON_RETRIES", 1), 0)
        working_ctx = getattr(runtime, "working_context", None) or {}
        is_reply_to_other = bool(
            getattr(runtime, "is_reply_to_other_user", False)
            or working_ctx.get("is_reply_to_other_user")
        )
        try:
            yield_fn = getattr(self, "_yield_to_user_replies", None)
            if callable(yield_fn):
                await yield_fn()
            result = await call_ollama_json(
                self._build_emotion_reason_prompt(runtime, emotion_key, appraisal=appraisal),
                system_prompt=EMOTION_REASONER_SYSTEM_PROMPT,
                think=False,
                model=_emotion_scorer_model_name(),
                schema=EMOTION_REASON_SCHEMA,
                timeout_sec=reason_timeout,
                retries=reason_retries,
                num_predict=max(cfg_int("EMOTION_REASON_NUM_PREDICT", 96), 32),
            )
            raw_reason = str(result.get("reason") or "").strip()
            if looks_like_bad_reason_text(raw_reason):
                log.warning("emotion reason rejected by looks_like_bad_reason_text: raw=%r emotion=%s", raw_reason, emotion_key)
                return _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars) or fallback_reason_text(emotion_key)
            eval_res = _emotion_evaluator.evaluate_emotion_state(
                scores={emotion_key: 1.0},
                raw_reason=raw_reason,
                max_chars=max_chars,
                is_reply_to_other_user=is_reply_to_other,
            )
            if not eval_res.is_valid or looks_like_bad_reason_text(eval_res.reason):
                log.warning("emotion reason rejected: raw=%r normalized=%r emotion=%s (%s)", raw_reason, eval_res.reason, emotion_key, eval_res.rejection_reason)
                return _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars) or eval_res.reason
            return normalize_reason_text(eval_res.reason, max_chars=max_chars)
        except asyncio.CancelledError:
            log.info(
                "emotion reason cancelled (semaphore wait or timeout); falling back to appraisal/fallback reason"
            )
            return (
                _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars)
                or _emotion_evaluator.fallback_reason_text(emotion_key, is_reply_to_other_user=is_reply_to_other)
            )
        except Exception as e:
            log.warning("emotion reason failed: %r", e)
            return (
                _reason_from_appraisal(appraisal, emotion_key, max_chars=max_chars)
                or _emotion_evaluator.fallback_reason_text(emotion_key, is_reply_to_other_user=is_reply_to_other)
            )


    async def _update_emotion_state(
        self,
        runtime: MessageRuntime,
        incoming_scores: dict[str, float] | None = None,
        incoming_appraisal: str | None = None,
        incoming_reaction: str | None = None,
    ) -> str:
        if not emotion_scoring_enabled():
            return "NONE"

        msg = getattr(runtime, "message", None)
        if msg is not None:
            bot_obj = getattr(self, "bot", None)
            bot_user_id = getattr(getattr(bot_obj, "user", None), "id", None)
            from ...common.bot_identity import is_message_addressed_to_other_bot
            if is_message_addressed_to_other_bot(msg, bot_user_id=bot_user_id, bot=bot_obj):
                log.debug("TRACE _update_emotion_state: message is addressed to another bot, skipping")
                return "NONE"

        max_appraisal_chars = max(int(cfg("EMOTION_APPRAISAL_MAX_CHARS", 80) or 80), 20)
        reaction_result = "NONE"
        if incoming_scores is None:
            try:
                incoming, appraisal, reaction_result = await self._score_emotion_with_appraisal(runtime)
            except asyncio.CancelledError:
                log.info("emotion state update cancelled during scoring; skipping detailed LLM update")
                return "NONE"
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
            dominant = self._pick_dominant_emotion(
                self.emotion_state.scores,
                incoming_scores=incoming,
                appraisal=appraisal,
            )
            self.emotion_state.dominant_emotion = dominant

            self.emotion_state.last_trigger_text = truncate_text(runtime.original_user_text, 120) or ""
            self.emotion_state.last_trigger_user_id = getattr(runtime, "user_id", None)
        working_ctx = getattr(runtime, "working_context", None) or {}
        is_reply_to_other = bool(
            getattr(runtime, "is_reply_to_other_user", False)
            or working_ctx.get("is_reply_to_other_user")
        )
        fallback_appraisal_key = dominant if dominant != "neutral" else _peak_emotion_key(incoming)
        max_reason_chars = max(int(cfg("EMOTION_REASON_MAX_CHARS", 70) or 70), 20)
        # 低シグナル時の前回理由・所感の引き継ぎ可否（harness.agent.observation.emotion_evaluatorへ委譲）
        # dominant が neutral / 非neutral のいずれでも、前回の理由・出来事評価が平常・時間減衰系なら絶対に引き継がない
        can_reuse_reason, can_reuse_appraisal = _emotion_evaluator.resolve_low_signal_reuse(
            used_low_signal_carry=used_low_signal_carry,
            dominant=dominant,
            previous_dominant=previous_dominant,
            previous_reason=previous_reason,
            previous_appraisal=previous_appraisal,
        )
        if used_low_signal_carry and dominant == previous_dominant:
            if can_reuse_appraisal:
                appraisal = previous_appraisal
            elif _looks_like_bad_appraisal_text(appraisal, is_reply_to_other_user=is_reply_to_other) or is_neutral_or_decayed_appraisal(appraisal):
                appraisal = _fallback_appraisal_text(fallback_appraisal_key, is_reply_to_other_user=is_reply_to_other)

            if can_reuse_reason:
                reason = previous_reason
            else:
                reason = await self._reason_emotion(runtime, dominant, appraisal=appraisal)
        else:
            if _looks_like_bad_appraisal_text(appraisal, is_reply_to_other_user=is_reply_to_other):
                appraisal = _fallback_appraisal_text(fallback_appraisal_key, is_reply_to_other_user=is_reply_to_other)
            reason = await self._reason_emotion(runtime, dominant, appraisal=appraisal)

        # 多重防御: 会話直後に時間減衰表現が残存した理由・所感や、主要感情と不整合な平常理由を修復
        # （harness.agent.observation.emotion_evaluatorへ委譲）
        appraisal, reason = _emotion_evaluator.repair_post_conversation_emotion_text(
            dominant=dominant,
            reason=reason,
            appraisal=appraisal,
            max_chars=max_reason_chars,
            is_reply_to_other_user=is_reply_to_other,
        )

        async with self._emotion_lock:
            self.emotion_state.appraisal = appraisal
            self.emotion_state.reason = reason
            self.emotion_state.last_trigger_user_id = getattr(runtime, "user_id", None)
            self._recompute_composite_indices(incoming, fatigue_delta=0.02)
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
                        persona=cfg_persona_namespace(),
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

            # 古いログのトリム呼び出し（最大50件保持）
            try:
                trim_fn = getattr(store, "trim_old_user_relationship_logs", None)
                if callable(trim_fn):
                    await trim_fn(
                        persona=cfg_persona_namespace(),
                        user_id=int(user_id_val),
                        keep_count=50,
                    )
            except Exception as e:
                log.debug("failed to trim old user relationship logs: %r", e)

        # 一言コメント（thought）の更新チェックとバックグラウンド生成トリガー
        if relationship_snapshot is not None and user_id_val is not None:
            if should_update_user_thought(relationship_snapshot, current_ts=time.time()):
                user_name = (
                    getattr(runtime, "author_display_name", "")
                    or getattr(runtime, "author_name", "")
                    or ""
                )
                delay_sec = max(float(cfg("RELATIONSHIP_THOUGHT_INITIAL_DELAY_SEC", 3.5) or 3.5), 0.0)
                asyncio.create_task(
                    self._update_user_thought_async(
                        int(user_id_val),
                        user_name=user_name,
                        initial_delay_sec=delay_sec,
                    )
                )

        trust = float(getattr(relationship_snapshot, "trust", 0.0)) if relationship_snapshot else 0.0
        affinity = float(getattr(relationship_snapshot, "affinity", 0.0)) if relationship_snapshot else 0.0
        affection = float(getattr(relationship_snapshot, "affection", 0.0)) if relationship_snapshot else 0.0
        hatred = float(getattr(relationship_snapshot, "hatred", 0.0)) if relationship_snapshot else 0.0

        async with self._emotion_lock:
            fatigue = getattr(self.emotion_state, 'fatigue', 0.0)
            tension = getattr(self.emotion_state, 'tension', 0.0)
            dominant = self.emotion_state.dominant_emotion

            self.emotion_state.action_policy = self._derive_action_policy(
                fatigue=fatigue,
                tension=tension,
                dominant=dominant,
                trust=trust,
                affinity=affinity,
                affection=affection,
                hatred=hatred,
                scores=incoming,
            )

        self._save_emotion_state()
        try:
            sync_presence = getattr(self, "_sync_emotion_presence", None)
            if callable(sync_presence):
                asyncio.create_task(sync_presence())
        except Exception as e:
            log.warning("presence sync scheduling failed: %r", e)
        return reaction_result if reaction_result in ("GOOD", "BAD") else "UPDATED"
