"""
[AI Agent Summary]
ユーザー習慣プロファイル、学習候補データ、フィードバック、内省（リフレクション）を担当するモジュール。
This module handles user habit profiles, training candidates, user feedback, and reflection beliefs.
"""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from ..json_compat import dumps as json_dumps
from .base import _MemoryStoreBase, utcnow_iso
from lib.db_utils import fetch_all_dicts, fetch_one_dict

if TYPE_CHECKING:
    _MemoryMixinBase = _MemoryStoreBase
else:
    _MemoryMixinBase = object

log = logging.getLogger(__name__)


class _MemoryLearningMixin(_MemoryMixinBase):
    async def add_training_candidate(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        user_text: str,
        assistant_text: str,
        context_text: str | None,
        reason: str | None = None,
    ) -> int:
        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                INSERT INTO training_candidates (
                    persona, guild_id, channel_id, user_id,
                    user_text, assistant_text, context_text,
                    accepted, rejected, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                persona, guild_id, channel_id, user_id,
                user_text, assistant_text, context_text,
                0, 0, reason, utcnow_iso(),
            ))
            return int(cur.lastrowid or 0)

    async def list_training_candidates(
        self,
        *,
        persona: str,
        accepted_only: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self._cursor() as cur:
            where_sql = "WHERE persona = ? AND accepted = 1" if accepted_only else "WHERE persona = ?"
            await cur.execute(f"""
                SELECT *
                FROM training_candidates
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona, int(limit)))
            return await fetch_all_dicts(cur)

    async def mark_training_candidate(
        self,
        candidate_id: int,
        *,
        accepted: bool | None = None,
        rejected: bool | None = None,
    ) -> None:
        fields = []
        values: list[Any] = []

        if accepted is not None:
            fields.append("accepted = ?")
            values.append(1 if accepted else 0)
        if rejected is not None:
            fields.append("rejected = ?")
            values.append(1 if rejected else 0)
        if not fields:
            return

        values.append(int(candidate_id))
        sql = f"UPDATE training_candidates SET {', '.join(fields)} WHERE id = ?"
        async with self._cursor(commit=True) as cur:
            await cur.execute(sql, values)

    async def upsert_user_habit_profile(
        self,
        *,
        persona: str,
        guild_id: int | None,
        user_id: int | None,
        tone_casual: float | None = None,
        response_detail: float | None = None,
        search_preference: float | None = None,
        joke_receptivity: float | None = None,
        alpha: float = 0.12,
        source_summary: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        await self._ensure_initialized()
        if user_id is None:
            raise ValueError("user_id is required for habit profile")

        now = utcnow_iso()
        persona = str(persona or "").strip() or "default"
        alpha = max(0.0, min(1.0, float(alpha)))
        source_payload = {
            "tone_casual": tone_casual,
            "response_detail": response_detail,
            "search_preference": search_preference,
            "joke_receptivity": joke_receptivity,
            "confidence": confidence,
            "source_summary": str(source_summary or "").strip()[:240],
        }

        def _signal(value: float | None) -> float | None:
            if value is None:
                return None
            return max(0.0, min(1.0, float(value)))

        signals = {
            "tone_casual": _signal(tone_casual),
            "response_detail": _signal(response_detail),
            "search_preference": _signal(search_preference),
            "joke_receptivity": _signal(joke_receptivity),
        }

        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                SELECT *
                FROM user_habit_profiles
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (persona, guild_id, int(user_id)),
            )
            row = await cur.fetchone()

            if row:
                values = {
                    "tone_casual": float(row["tone_casual"]),
                    "response_detail": float(row["response_detail"]),
                    "search_preference": float(row["search_preference"]),
                    "joke_receptivity": float(row["joke_receptivity"]),
                }
                for key, signal in signals.items():
                    if signal is not None:
                        values[key] = values[key] * (1.0 - alpha) + signal * alpha
                evidence_count = int(row["evidence_count"] or 0) + 1
                await cur.execute(
                    """
                    UPDATE user_habit_profiles
                    SET tone_casual = ?,
                        response_detail = ?,
                        search_preference = ?,
                        joke_receptivity = ?,
                        evidence_count = ?,
                        last_signals_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        values["tone_casual"],
                        values["response_detail"],
                        values["search_preference"],
                        values["joke_receptivity"],
                        evidence_count,
                        json_dumps(source_payload, ensure_ascii=False),
                        now,
                        int(row["id"]),
                    ),
                )
                return {
                    "id": int(row["id"]),
                    "persona": persona,
                    "guild_id": guild_id,
                    "user_id": int(user_id),
                    **values,
                    "evidence_count": evidence_count,
                    "last_signals_json": json_dumps(source_payload, ensure_ascii=False),
                    "created_at": row["created_at"],
                    "updated_at": now,
                }

            values = {}
            for key, signal in signals.items():
                old_value = 0.5
                values[key] = old_value if signal is None else old_value * (1.0 - alpha) + signal * alpha
            await cur.execute(
                """
                INSERT INTO user_habit_profiles (
                    persona, guild_id, user_id,
                    tone_casual, response_detail, search_preference, joke_receptivity,
                    evidence_count, last_signals_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona,
                    guild_id,
                    int(user_id),
                    values["tone_casual"],
                    values["response_detail"],
                    values["search_preference"],
                    values["joke_receptivity"],
                    1,
                    json_dumps(source_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return {
                "id": int(cur.lastrowid or 0),
                "persona": persona,
                "guild_id": guild_id,
                "user_id": int(user_id),
                **values,
                "evidence_count": 1,
                "last_signals_json": json_dumps(source_payload, ensure_ascii=False),
                "created_at": now,
                "updated_at": now,
            }

    async def get_user_habit_profile(
        self,
        *,
        persona: str,
        guild_id: int | None,
        user_id: int | None,
    ) -> dict[str, Any] | None:
        if user_id is None:
            return None

        async with self._cursor() as cur:
            await cur.execute(
                """
                SELECT *
                FROM user_habit_profiles
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND user_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (str(persona or "").strip() or "default", guild_id, int(user_id)),
            )
            return await fetch_one_dict(cur)

    async def add_feedback_item(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        original_user_text: str,
        assistant_text: str,
        feedback_text: str,
        corrected_assistant_text: str | None = None,
        feedback_type: str | None = None,
        accepted: bool = False,
    ) -> int:
        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                INSERT INTO feedback_items (
                    persona, guild_id, channel_id, user_id,
                    original_user_text, assistant_text, feedback_text,
                    corrected_assistant_text, feedback_type,
                    accepted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                persona, guild_id, channel_id, user_id,
                original_user_text, assistant_text, feedback_text,
                corrected_assistant_text, feedback_type,
                1 if accepted else 0, utcnow_iso(),
            ))
            return int(cur.lastrowid or 0)

    async def list_feedback_items(
        self,
        *,
        persona: str,
        accepted_only: bool = False,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        async with self._cursor() as cur:
            where_sql = "WHERE persona = ? AND accepted = 1" if accepted_only else "WHERE persona = ?"
            await cur.execute(f"""
                SELECT *
                FROM feedback_items
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?
            """, (persona, int(limit)))
            return await fetch_all_dicts(cur)

    async def mark_feedback_item(
        self,
        feedback_id: int,
        *,
        accepted: bool,
        corrected_assistant_text: str | None = None,
    ) -> None:
        async with self._cursor(commit=True) as cur:
            if corrected_assistant_text is None:
                await cur.execute("""
                    UPDATE feedback_items
                    SET accepted = ?
                    WHERE id = ?
                """, (1 if accepted else 0, int(feedback_id)))
            else:
                await cur.execute("""
                    UPDATE feedback_items
                    SET accepted = ?, corrected_assistant_text = ?
                    WHERE id = ?
                """, (1 if accepted else 0, corrected_assistant_text, int(feedback_id)))

    async def add_reflection(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        subject_kind: str,
        subject_key: str,
        concept: str,
        belief: str,
        importance_score: float,
        evidence_count: int = 0,
        source_window_start: str | None = None,
        source_window_end: str | None = None,
    ) -> int:
        await self._ensure_initialized()
        now = utcnow_iso()

        persona = str(persona or "").strip() or "default"
        subject_kind = str(subject_kind or "").strip() or "agent"
        subject_key = str(subject_key or "").strip() or "core"
        concept = str(concept or "").strip()
        belief = str(belief or "").strip()
        if not concept:
            raise ValueError("empty reflection concept")
        if not belief:
            raise ValueError("empty reflection belief")
        importance_score = max(0.0, min(1.0, float(importance_score)))
        evidence_count = max(0, int(evidence_count))
        source_window_start = str(source_window_start or "").strip() or None
        source_window_end = str(source_window_end or "").strip() or None

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                SELECT id, importance_score, evidence_count
                FROM reflections
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND COALESCE(channel_id, -1) = COALESCE(?, -1)
                  AND subject_kind = ?
                  AND subject_key = ?
                  AND concept = ?
                  AND belief = ?
                LIMIT 1
            """, (
                persona, guild_id, channel_id,
                subject_kind, subject_key, concept, belief,
            ))
            row = await cur.fetchone()

            if row:
                await cur.execute("""
                    UPDATE reflections
                    SET importance_score = ?,
                        evidence_count = ?,
                        source_window_start = COALESCE(?, source_window_start),
                        source_window_end = COALESCE(?, source_window_end),
                        created_at = ?
                    WHERE id = ?
                """, (
                    max(importance_score, float(row["importance_score"])),
                    max(evidence_count, int(row["evidence_count"])),
                    source_window_start,
                    source_window_end,
                    now,
                    row["id"],
                ))
                return int(row["id"])

            await cur.execute("""
                INSERT INTO reflections (
                    persona, guild_id, channel_id,
                    subject_kind, subject_key,
                    concept, belief, importance_score, evidence_count,
                    source_window_start, source_window_end,
                    created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                persona, guild_id, channel_id,
                subject_kind, subject_key,
                concept, belief, importance_score, evidence_count,
                source_window_start, source_window_end,
                now, None,
            ))
            return int(cur.lastrowid or 0)

    async def list_reflections(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        subject_kind: str = "agent",
        subject_key: str = "core",
        limit: int = 3,
        min_importance: float = 0.0,
    ) -> list[dict[str, Any]]:
        async with self._cursor() as cur:
            await cur.execute("""
                SELECT *
                FROM reflections
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND (
                        COALESCE(channel_id, -1) = COALESCE(?, -1)
                        OR COALESCE(channel_id, -1) = -1
                      )
                  AND subject_kind = ?
                  AND subject_key = ?
                  AND importance_score >= ?
                ORDER BY importance_score DESC, created_at DESC
                LIMIT ?
            """, (
                persona, guild_id, channel_id,
                subject_kind, subject_key,
                float(min_importance), int(limit),
            ))
            return await fetch_all_dicts(cur)

    async def touch_reflection(self, reflection_id: int) -> None:
        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                UPDATE reflections
                SET last_used_at = ?
                WHERE id = ?
            """, (utcnow_iso(), int(reflection_id)))
