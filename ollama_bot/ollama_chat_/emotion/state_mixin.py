"""
[AI Agent Summary]
このファイルは、感情状態や関係値の永続化（保存と読み込み）、および時間経過による感情の減衰ループを担当する Mixin です。
This mixin handles persistence (save/load) of emotion states and relationships, as well as the emotion decay loop over time.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, TYPE_CHECKING

from lib.json_utils import load_json_async, save_json_atomic, save_json_atomic_async

from ...common.config_helpers import cfg_float
from ...common.emotion_helpers import EMOTION_KEYS, clamp_score, fallback_reason_text
from ..ollama_chat_types import UserRelationship

log = logging.getLogger("ollama_bot.ollama_chat")

from .utils import (
    _fallback_appraisal_text,
    _load_persisted_timestamp,
    is_neutral_or_decayed_appraisal,
    is_neutral_or_decayed_reason,
)
from .relationships_logic import _copy_relationship, _clamp_relationship_value
from .relationships_thought_logic import should_update_user_thought, looks_like_placeholder_thought

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _EmotionStateBase = OllamaChatProtocol
else:
    _EmotionStateBase = object


class OllamaChatEmotionStateMixin(_EmotionStateBase):
    def _ensure_relationship_state(self) -> None:
        if getattr(self, "user_relationships", None) is None:
            self.user_relationships: dict[int, UserRelationship] = {}
        if getattr(self, "_relationship_lock", None) is None:
            self._relationship_lock = asyncio.Lock()

    def _get_state_file_suffix(self) -> str:
        config_name = str(os.environ.get("OLLAMA_BOT_CONFIG", "default") or "default").strip() or "default"
        return re.sub(r"[^0-9A-Za-z_.-]+", "_", config_name)

    def _get_emotion_state_file(self) -> str:
        return f"emotion_state_{self._get_state_file_suffix()}.json"

    def _get_user_relationships_file(self) -> str:
        return f"user_relationships_{self._get_state_file_suffix()}.json"

    def _save_emotion_state(self) -> None:
        try:
            now_ts = time.time()
            payload = {
                "scores": {key: clamp_score(float(self.emotion_state.scores.get(key, 0.0))) for key in EMOTION_KEYS},
                "dominant_emotion": str(self.emotion_state.dominant_emotion or "neutral"),
                "appraisal": str(getattr(self.emotion_state, "appraisal", "") or ""),
                "reason": str(self.emotion_state.reason or ""),
                "mood": str(self.emotion_state.mood or "neutral"),
                "mood_reason": str(self.emotion_state.mood_reason or ""),
                "boredom": clamp_score(float(self.emotion_state.boredom or 0.0)),
                "loneliness": clamp_score(float(self.emotion_state.loneliness or 0.0)),
                "curiosity": clamp_score(float(self.emotion_state.curiosity or 0.0)),
                "tension": clamp_score(float(self.emotion_state.tension or 0.0)),
                "fatigue": clamp_score(float(self.emotion_state.fatigue or 0.0)),
                "vigilance": clamp_score(float(self.emotion_state.vigilance or 0.0)),
                "shame": clamp_score(float(self.emotion_state.shame or 0.0)),
                "excitement": clamp_score(float(self.emotion_state.excitement or 0.0)),
                "action_policy": str(getattr(self.emotion_state, "action_policy", "") or ""),
                "last_thought_ts": float(self.emotion_state.last_thought_ts or 0.0),
                "last_human_message_ts": float(self.emotion_state.last_human_message_ts or 0.0),
                "last_proactive_post_ts": float(self.emotion_state.last_proactive_post_ts or 0.0),
                "last_agent_tick_ts": float(self.emotion_state.last_agent_tick_ts or 0.0),
                "recent_topic_hint": str(self.emotion_state.recent_topic_hint or ""),
                "last_trigger_text": str(self.emotion_state.last_trigger_text or ""),
                "last_decay_ts": float(self.emotion_state.last_decay_ts or 0.0),
                "base_display_name": str(self.emotion_state.base_display_name or ""),
                "last_save_ts": now_ts,
            }
            filepath = self._get_emotion_state_file()
            save_json_atomic(filepath, payload)
        except Exception as e:
            log.warning("感情ステータスの保存に失敗しました: %r", e)

    async def _load_emotion_state(self) -> None:
        file_path = self._get_emotion_state_file()
        try:
            saved_state = await load_json_async(file_path)
            if not isinstance(saved_state, dict):
                return
            
            now_timestamp = time.time()
            last_save_ts = float(saved_state.get("last_save_ts", 0.0) or 0.0)
            if last_save_ts <= 0.0:
                last_save_ts = float(saved_state.get("last_decay_ts", 0.0) or 0.0)
            
            # 停止していた経過時間（秒）を計算
            elapsed_sec = max(0.0, now_timestamp - last_save_ts) if last_save_ts > 0.0 else 0.0
            elapsed_hours = elapsed_sec / 3600.0
            
            decay_ratio_per_hour = max(0.0, min(cfg_float("EMOTION_HOURLY_DECAY_RATIO", 0.90), 1.0))
            offline_decay = (decay_ratio_per_hour ** elapsed_hours) if elapsed_hours > 0.0 else 1.0
            
            saved_scores = saved_state.get("scores", {}) if isinstance(saved_state, dict) else {}
            if isinstance(saved_scores, dict):
                for key in EMOTION_KEYS:
                    if key in saved_scores:
                        raw_val = clamp_score(float(saved_scores.get(key, 0.0)))
                        self.emotion_state.scores[key] = clamp_score(raw_val * offline_decay)
            
            dominant = self._pick_dominant_emotion(self.emotion_state.scores)
            self.emotion_state.dominant_emotion = dominant
            
            # 2時間以上オフラインだった、または減衰の結果 neutral に戻った場合は平常状態にリセット
            if dominant == "neutral" or elapsed_hours >= 2.0:
                self.emotion_state.dominant_emotion = "neutral"
                self.emotion_state.appraisal = "平常状態"
                self.emotion_state.reason = "平常状態"
                self.emotion_state.mood = "neutral"
                self.emotion_state.mood_reason = "大きな偏りがないため"
            else:
                loaded_appraisal = str(saved_state.get("appraisal", "") or "").strip() or "平常状態"
                loaded_reason = str(saved_state.get("reason", "") or "").strip() or "平常状態"
                # 主要感情がアクティブ（非neutral）なのに保存理由・所感が平常・減衰系の場合は自動修復
                if dominant != "neutral":
                    if is_neutral_or_decayed_reason(loaded_reason):
                        loaded_reason = fallback_reason_text(dominant)
                    if is_neutral_or_decayed_appraisal(loaded_appraisal):
                        loaded_appraisal = _fallback_appraisal_text(dominant)
                self.emotion_state.appraisal = loaded_appraisal
                self.emotion_state.reason = loaded_reason
                self.emotion_state.mood = str(saved_state.get("mood", "neutral") or "neutral").strip() or "neutral"
                self.emotion_state.mood_reason = str(saved_state.get("mood_reason", "") or "").strip() or "特になし"
            
            try:
                self.emotion_state.boredom = clamp_score(float(saved_state.get("boredom", 0.0) or 0.0) * offline_decay)
                self.emotion_state.loneliness = clamp_score(float(saved_state.get("loneliness", 0.0) or 0.0) * offline_decay)
                self.emotion_state.curiosity = clamp_score(float(saved_state.get("curiosity", 0.0) or 0.0) * offline_decay)
                self.emotion_state.tension = clamp_score(float(saved_state.get("tension", 0.0) or 0.0) * offline_decay)
                self.emotion_state.fatigue = clamp_score(float(saved_state.get("fatigue", 0.0) or 0.0) * offline_decay)
                self.emotion_state.vigilance = clamp_score(float(saved_state.get("vigilance", 0.0) or 0.0) * offline_decay)
                self.emotion_state.shame = clamp_score(float(saved_state.get("shame", 0.0) or 0.0) * offline_decay)
                self.emotion_state.excitement = clamp_score(float(saved_state.get("excitement", 0.0) or 0.0) * offline_decay)
                self.emotion_state.action_policy = str(saved_state.get("action_policy", "") or "")
                
                self.emotion_state.last_thought_ts = _load_persisted_timestamp(
                    saved_state.get("last_thought_ts", 0.0),
                    now=now_timestamp,
                    field_name="last_thought_ts",
                    default_on_old=0.0,
                )
                self.emotion_state.last_human_message_ts = _load_persisted_timestamp(
                    saved_state.get("last_human_message_ts", 0.0),
                    now=now_timestamp,
                    field_name="last_human_message_ts",
                    default_on_old=0.0,
                )
                self.emotion_state.last_proactive_post_ts = _load_persisted_timestamp(
                    saved_state.get("last_proactive_post_ts", 0.0),
                    now=now_timestamp,
                    field_name="last_proactive_post_ts",
                    default_on_old=0.0,
                )
                self.emotion_state.last_agent_tick_ts = _load_persisted_timestamp(
                    saved_state.get("last_agent_tick_ts", 0.0),
                    now=now_timestamp,
                    field_name="last_agent_tick_ts",
                    default_on_old=now_timestamp,
                )
            except Exception:
                self.emotion_state.boredom = 0.0
                self.emotion_state.loneliness = 0.0
                self.emotion_state.curiosity = 0.0
                self.emotion_state.tension = 0.0
                self.emotion_state.fatigue = 0.0
                self.emotion_state.vigilance = 0.0
                self.emotion_state.shame = 0.0
                self.emotion_state.excitement = 0.0
                self.emotion_state.action_policy = ""
                self.emotion_state.last_thought_ts = 0.0
                self.emotion_state.last_human_message_ts = 0.0
                self.emotion_state.last_proactive_post_ts = 0.0
                self.emotion_state.last_agent_tick_ts = 0.0
            
            self.emotion_state.recent_topic_hint = str(saved_state.get("recent_topic_hint", "") or "").strip()
            self.emotion_state.last_trigger_text = str(saved_state.get("last_trigger_text", "") or "")
            try:
                self.emotion_state.last_decay_ts = _load_persisted_timestamp(
                    saved_state.get("last_decay_ts", 0.0),
                    now=now_timestamp,
                    field_name="last_decay_ts",
                    default_on_old=0.0,
                )
            except Exception:
                self.emotion_state.last_decay_ts = 0.0
            saved_base_name = str(saved_state.get("base_display_name", "") or "").strip()
            if saved_base_name:
                self.emotion_state.base_display_name = saved_base_name
            log.info("感情ステータスを復元しました: %s (elapsed=%.1fh decay=%.3f dominant=%s)", file_path, elapsed_hours, offline_decay, self.emotion_state.dominant_emotion)
        except Exception as e:
            log.warning("感情ステータスの復元に失敗しました: %r", e)

    async def _save_user_relationships(self) -> None:
        self._ensure_relationship_state()
        try:
            async with self._relationship_lock:
                payload = {
                    "users": {
                        str(int(user_id)): {
                            "affinity": _clamp_relationship_value(rel.affinity),
                            "trust": _clamp_relationship_value(rel.trust),
                            "affection": _clamp_relationship_value(getattr(rel, "affection", 0.0) or 0.0),
                            "hatred": _clamp_relationship_value(getattr(rel, "hatred", 0.0) or 0.0),
                            "thought": str(getattr(rel, "thought", "") or "").strip(),
                            "last_interaction_ts": float(getattr(rel, "last_interaction_ts", 0.0) or 0.0),
                            "last_thought_generated_ts": float(getattr(rel, "last_thought_generated_ts", 0.0) or 0.0),
                            "interaction_count": int(getattr(rel, "interaction_count", 0) or 0),
                        }
                        for user_id, rel in self.user_relationships.items()
                    }
                }
            filepath = self._get_user_relationships_file()
            await save_json_atomic_async(filepath, payload)
        except Exception as e:
            log.warning("関係ベクトルの保存に失敗しました: %r", e)

    async def _load_user_relationships(self) -> None:
        self._ensure_relationship_state()
        file_path = self._get_user_relationships_file()
        try:
            saved_state = await load_json_async(file_path)
            saved_users = saved_state.get("users", {}) if isinstance(saved_state, dict) else {}
            restored: dict[int, UserRelationship] = {}
            if isinstance(saved_users, dict):
                for raw_user_id, raw_rel in saved_users.items():
                    if not isinstance(raw_rel, dict):
                        continue
                    try:
                        user_id = int(raw_user_id)
                    except Exception:
                        continue
                    loaded_thought = str(raw_rel.get("thought", "") or "").strip()
                    if looks_like_placeholder_thought(loaded_thought):
                        loaded_thought = ""
                    restored[user_id] = UserRelationship(
                        affinity=_clamp_relationship_value(raw_rel.get("affinity", 0.0)),
                        trust=_clamp_relationship_value(raw_rel.get("trust", 0.0)),
                        affection=_clamp_relationship_value(raw_rel.get("affection", 0.0)),
                        hatred=_clamp_relationship_value(raw_rel.get("hatred", 0.0)),
                        thought=loaded_thought,
                        last_interaction_ts=float(raw_rel.get("last_interaction_ts", 0.0) or 0.0),
                        last_thought_generated_ts=float(raw_rel.get("last_thought_generated_ts", 0.0) or 0.0),
                        interaction_count=int(raw_rel.get("interaction_count", 0) or 0),
                    )
            self.user_relationships = restored
            log.info("関係ベクトルを復元しました: %s users=%s", file_path, len(restored))
        except Exception as e:
            log.warning("関係ベクトルの復元に失敗しました: %r", e)

    async def _emotion_decay_loop(self) -> None:
        await self.bot.wait_until_ready()
        interval_sec = max(cfg_float("EMOTION_HOURLY_DECAY_SEC", 3600.0), 60.0)
        decay_ratio = max(0.0, min(cfg_float("EMOTION_HOURLY_DECAY_RATIO", 0.90), 1.0))
        while not self.bot.is_closed():
            await asyncio.sleep(interval_sec)
            try:
                changed = False
                async with self._emotion_lock:
                    for key in EMOTION_KEYS:
                        current = clamp_score(self.emotion_state.scores.get(key, 0.0))
                        updated = clamp_score(current * decay_ratio)
                        if abs(updated - current) >= 0.001:
                            changed = True
                        self.emotion_state.scores[key] = updated
                    self.emotion_state.tension = clamp_score(getattr(self.emotion_state, 'tension', 0.0) * decay_ratio)
                    self.emotion_state.fatigue = clamp_score(getattr(self.emotion_state, 'fatigue', 0.0) * decay_ratio)
                    self.emotion_state.vigilance = clamp_score(getattr(self.emotion_state, 'vigilance', 0.0) * decay_ratio)
                    self.emotion_state.shame = clamp_score(getattr(self.emotion_state, 'shame', 0.0) * decay_ratio)
                    self.emotion_state.excitement = clamp_score(getattr(self.emotion_state, 'excitement', 0.0) * decay_ratio)
                    self.emotion_state.dominant_emotion = self._pick_dominant_emotion(self.emotion_state.scores)
                    if self.emotion_state.dominant_emotion == "neutral":
                        self.emotion_state.appraisal = "時間が経って、出来事の引っかかりが薄れてきた。"
                        self.emotion_state.reason = "時間が経って、少し落ち着いてきたためです。"
                    elif is_neutral_or_decayed_reason(self.emotion_state.reason):
                        # 主要感情がアクティブなのに理由が平常系のまま残っていたら修復
                        self.emotion_state.reason = fallback_reason_text(self.emotion_state.dominant_emotion)
                        if is_neutral_or_decayed_appraisal(getattr(self.emotion_state, "appraisal", "")):
                            self.emotion_state.appraisal = _fallback_appraisal_text(self.emotion_state.dominant_emotion)
                    self.emotion_state.last_decay_ts = time.time()
                if changed:
                    self._save_emotion_state()
                    await self._sync_emotion_presence()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("emotion decay loop failed: %r", e)

    async def _relationship_thought_loop(self) -> None:
        """定期的に最近アクティブなユーザーの関係性・一言所感を巡回し、必要に応じてLLMで更新する。"""
        await self.bot.wait_until_ready()
        interval_sec = max(cfg_float("RELATIONSHIP_THOUGHT_LOOP_INTERVAL_SEC", 3600.0), 60.0)
        while not self.bot.is_closed():
            await asyncio.sleep(interval_sec)
            try:
                self._ensure_relationship_state()
                now = time.time()
                async with self._relationship_lock:
                    user_ids = list(self.user_relationships.keys())

                update_fn = getattr(self, "_update_user_thought_async", None)
                if not callable(update_fn):
                    continue

                for uid in user_ids:
                    async with self._relationship_lock:
                        rel = self.user_relationships.get(uid)
                        if rel is None:
                            continue
                        rel_copy = _copy_relationship(rel)

                    # 過去48時間以内に対話があったアクティブなユーザーで、更新条件を満たすものを対象とする
                    if now - rel_copy.last_interaction_ts > 86400.0 * 2:
                        continue
                    if should_update_user_thought(rel_copy, current_ts=now):
                        await update_fn(uid)
                        # 各ユーザー生成の間に少しディレイを入れてLLM負荷を分散
                        await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("relationship thought loop failed: %r", e)
