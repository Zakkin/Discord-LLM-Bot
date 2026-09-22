"""
[AI Agent Summary]
Bot発話ログ、チャンネル要約、エピソード記憶、想起（Prospective memory）を担当するモジュール。
This module handles bot utterances, channel summaries, episodic memories, and prospective memory management.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING

from ..json_compat import dumps as json_dumps, loads as json_loads
from .base import _MemoryStoreBase, utcnow_iso
from lib.db_utils import fetch_all_dicts, fetch_one_dict

if TYPE_CHECKING:
    _MemoryMixinBase = _MemoryStoreBase
else:
    _MemoryMixinBase = object

log = logging.getLogger(__name__)


class _MemoryInteractionsMixin(_MemoryMixinBase):
    async def add_bot_utterance(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        content: str,
    ) -> int:
        persona = str(persona or "").strip() or "default"
        content = str(content or "").strip()
        if not content:
            raise ValueError("empty bot utterance")
        content = content[:2000]

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                INSERT INTO bot_utterances (
                    persona, guild_id, channel_id, content, created_at
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                persona,
                guild_id,
                channel_id,
                content,
                utcnow_iso(),
            ))
            return int(cur.lastrowid or 0)

    async def get_recent_bot_utterances(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        limit: int = 3,
        max_age_sec: float | None = None,
    ) -> list[str]:
        persona_str = str(persona or "").strip() or "default"
        query = """
            SELECT content
            FROM bot_utterances
            WHERE persona = ?
              AND COALESCE(guild_id, -1) = COALESCE(?, -1)
              AND COALESCE(channel_id, -1) = COALESCE(?, -1)
        """
        params: list[Any] = [persona_str, guild_id, channel_id]
        if max_age_sec is not None and max_age_sec > 0:
            since_dt = datetime.now(timezone.utc) - timedelta(seconds=float(max_age_sec))
            since_iso = since_dt.replace(microsecond=0).isoformat()
            query += " AND created_at >= ?"
            params.append(since_iso)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))

        async with self._cursor() as cur:
            await cur.execute(query, tuple(params))
            rows = list(await cur.fetchall())
            return [str(row["content"]) for row in reversed(rows)]

    async def set_channel_summary(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int,
        summary: str,
        source_count: int,
    ) -> None:
        now = utcnow_iso()
        persona = str(persona or "").strip() or "default"
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("empty channel summary")
        source_count = max(0, int(source_count))

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                SELECT id
                FROM channel_summaries
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND channel_id = ?
                LIMIT 1
            """, (persona, guild_id, channel_id))
            row = await cur.fetchone()

            if row:
                await cur.execute("""
                    UPDATE channel_summaries
                    SET summary = ?, source_count = ?, updated_at = ?
                    WHERE id = ?
                """, (summary, source_count, now, row["id"]))
            else:
                await cur.execute("""
                    INSERT INTO channel_summaries (
                        persona, guild_id, channel_id, summary, source_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (persona, guild_id, channel_id, summary, source_count, now))

    async def get_channel_summary(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int,
    ) -> dict[str, Any] | None:
        async with self._cursor() as cur:
            await cur.execute("""
                SELECT *
                FROM channel_summaries
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND channel_id = ?
                LIMIT 1
            """, (persona, guild_id, channel_id))
            return await fetch_one_dict(cur)

    async def save_channel_thread(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int,
        thread_key: str,
        topic: str,
        participants: list[str],
        turns: list[dict[str, Any]],
        last_active_ts: float,
    ) -> None:
        await self._ensure_initialized()
        now = utcnow_iso()
        participants_str = json_dumps(participants)
        turns_json = json_dumps(turns)

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                SELECT id
                FROM channel_threads
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND channel_id = ?
                  AND thread_key = ?
                LIMIT 1
            """, (persona, guild_id, channel_id, thread_key))
            row = await cur.fetchone()

            if row:
                await cur.execute("""
                    UPDATE channel_threads
                    SET topic = ?, participants = ?, turns_json = ?, last_active_ts = ?, updated_at = ?
                    WHERE id = ?
                """, (topic, participants_str, turns_json, last_active_ts, now, row["id"]))
            else:
                await cur.execute("""
                    INSERT INTO channel_threads (
                        persona, guild_id, channel_id, thread_key, topic, participants, turns_json, last_active_ts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (persona, guild_id, channel_id, thread_key, topic, participants_str, turns_json, last_active_ts, now, now))

    async def get_active_channel_threads(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int,
        min_active_ts: float,
    ) -> list[dict[str, Any]]:
        await self._ensure_initialized()
        async with self._cursor() as cur:
            await cur.execute("""
                SELECT *
                FROM channel_threads
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND channel_id = ?
                  AND last_active_ts >= ?
                ORDER BY last_active_ts DESC
            """, (persona, guild_id, channel_id, min_active_ts))
            rows = await fetch_all_dicts(cur)
            results = []
            for r in rows:
                d = dict(r)
                try:
                    d["participants"] = json_loads(str(d.get("participants") or "[]"))
                except Exception:
                    d["participants"] = []
                try:
                    d["turns"] = json_loads(str(d.get("turns_json") or "[]"))
                except Exception:
                    d["turns"] = []
                results.append(d)
            return results

    async def add_episode(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        summary: str,
        what: str | None = None,
        bot_action: str | None = None,
        user_reaction: str | None = None,
        emotion_before: dict[str, Any] | None = None,
        emotion_after: dict[str, Any] | None = None,
        importance: float = 0.5,
        tags: list[str] | None = None,
        created_at: str | None = None,
        promoted_at: str | None = None,
    ) -> int:
        await self._ensure_initialized()

        persona = str(persona or "").strip() or "default"
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("empty episode summary")

        normalized_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        normalized_importance = max(0.0, min(1.0, float(importance)))
        created = str(created_at or utcnow_iso()).strip() or utcnow_iso()

        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                INSERT INTO memory_episodes (
                    persona, guild_id, channel_id, user_id,
                    summary, what, bot_action, user_reaction,
                    emotion_before_json, emotion_after_json,
                    importance, tags_json, created_at, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona,
                    guild_id,
                    channel_id,
                    user_id,
                    summary[:280],
                    str(what or "").strip()[:600] or None,
                    str(bot_action or "").strip()[:600] or None,
                    str(user_reaction or "").strip()[:600] or None,
                    json_dumps(emotion_before, ensure_ascii=False) if emotion_before else None,
                    json_dumps(emotion_after, ensure_ascii=False) if emotion_after else None,
                    normalized_importance,
                    json_dumps(normalized_tags, ensure_ascii=False) if normalized_tags else None,
                    created,
                    str(promoted_at or "").strip() or None,
                ),
            )
            return int(cur.lastrowid or 0)

    async def list_recent_episodes(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        user_id: int | None = None,
        limit: int = 5,
        min_importance: float = 0.0,
        promoted_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [str(persona or "").strip() or "default", guild_id, float(min_importance)]
        where = [
            "persona = ?",
            "COALESCE(guild_id, -1) = COALESCE(?, -1)",
            "importance >= ?",
        ]
        if channel_id is not None:
            where.append("(COALESCE(channel_id, -1) = COALESCE(?, -1) OR channel_id IS NULL)")
            params.append(channel_id)
        if user_id is not None:
            where.append("(COALESCE(user_id, -1) = COALESCE(?, -1) OR user_id IS NULL)")
            params.append(user_id)
        if promoted_only is True:
            where.append("promoted_at IS NOT NULL")
        elif promoted_only is False:
            where.append("promoted_at IS NULL")
        params.append(max(int(limit), 1))

        async with self._cursor() as cur:
            await cur.execute(
                f"""
                SELECT *
                FROM memory_episodes
                WHERE {' AND '.join(where)}
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
                """,
                tuple(params),
            )
            return await fetch_all_dicts(cur)

    async def add_prospective_memory(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        target_user_id: int | None,
        kind: str = "event_based",
        cue_keywords: list[str] | None = None,
        cue_topic_tag: str | None = None,
        intent: str,
        priority: float = 0.5,
        expires_at: str | None = None,
        status: str = "pending",
    ) -> int:
        persona = str(persona or "").strip() or "default"
        intent = str(intent or "").strip()
        if not intent:
            raise ValueError("empty prospective intent")

        normalized_keywords = [str(v).strip() for v in (cue_keywords or []) if str(v).strip()]
        normalized_kind = str(kind or "event_based").strip() or "event_based"
        normalized_status = str(status or "pending").strip() or "pending"

        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                INSERT INTO memory_prospective (
                    persona, guild_id, channel_id, target_user_id,
                    kind, cue_keywords_json, cue_topic_tag, intent,
                    priority, expires_at, status, created_at, triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    persona,
                    guild_id,
                    channel_id,
                    target_user_id,
                    normalized_kind[:32],
                    json_dumps(normalized_keywords, ensure_ascii=False) if normalized_keywords else None,
                    str(cue_topic_tag or "").strip()[:80] or None,
                    intent[:400],
                    max(0.0, min(1.0, float(priority))),
                    str(expires_at or "").strip() or None,
                    normalized_status[:24],
                    utcnow_iso(),
                ),
            )
            return int(cur.lastrowid or 0)

    async def list_pending_prospective_memories(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        target_user_id: int | None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        async with self._cursor() as cur:
            await cur.execute(
                """
                SELECT *
                FROM memory_prospective
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND status = 'pending'
                  AND (
                        COALESCE(channel_id, -1) = COALESCE(?, -1)
                        OR channel_id IS NULL
                  )
                  AND (
                        COALESCE(target_user_id, -1) = COALESCE(?, -1)
                        OR target_user_id IS NULL
                  )
                ORDER BY priority DESC, created_at DESC
                LIMIT ?
                """,
                (
                    str(persona or "").strip() or "default",
                    guild_id,
                    channel_id,
                    target_user_id,
                    max(int(limit), 1),
                ),
            )
            return await fetch_all_dicts(cur)

    async def mark_prospective_triggered(self, record_id: int) -> None:
        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                UPDATE memory_prospective
                SET status = 'done', triggered_at = ?
                WHERE id = ?
                """,
                (utcnow_iso(), int(record_id)),
            )

    async def expire_prospective_memories(self, *, persona: str) -> int:
        now = utcnow_iso()
        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                UPDATE memory_prospective
                SET status = 'expired'
                WHERE persona = ?
                  AND status = 'pending'
                  AND expires_at IS NOT NULL
                  AND expires_at <> ''
                  AND expires_at < ?
                """,
                (str(persona or "").strip() or "default", now),
            )
            return int(cur.rowcount or 0)

    async def record_user_relationship_log(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        user_name: str | None = None,
        user_text: str,
        bot_appraisal: str | None = None,
        dominant_emotion: str | None = None,
        emotion_scores: dict[str, float] | None = None,
        affinity: float = 0.0,
        trust: float = 0.0,
        affection: float = 0.0,
        hatred: float = 0.0,
        max_logs_per_user: int = 50,
    ) -> int:
        """ユーザーとの対話内容とBotの心情評価・関係値をログに記録する。"""
        persona_val = str(persona or "default").strip()
        user_text_val = str(user_text or "").strip()[:1000]
        if not user_text_val:
            return 0
        appraisal_val = str(bot_appraisal or "").strip()[:500] if bot_appraisal else None
        dominant_val = str(dominant_emotion or "").strip() or None
        scores_json = json_dumps(emotion_scores) if emotion_scores else None
        now_iso = utcnow_iso()

        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                INSERT INTO user_relationship_logs (
                    persona, guild_id, channel_id, user_id, user_name,
                    user_text, bot_appraisal, dominant_emotion, emotion_scores_json,
                    affinity, trust, affection, hatred, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_val,
                    guild_id,
                    channel_id,
                    int(user_id),
                    str(user_name or "").strip() or None,
                    user_text_val,
                    appraisal_val,
                    dominant_val,
                    scores_json,
                    float(affinity),
                    float(trust),
                    float(affection),
                    float(hatred),
                    now_iso,
                ),
            )
            inserted_id = int(cur.lastrowid or 0)

        if max_logs_per_user > 0:
            try:
                await self.trim_old_user_relationship_logs(
                    persona=persona_val,
                    user_id=int(user_id),
                    keep_count=max_logs_per_user,
                )
            except Exception as e:
                log.debug("trim_old_user_relationship_logs error: %r", e)

        return inserted_id

    async def get_recent_user_relationship_logs(
        self,
        *,
        persona: str,
        user_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """指定ユーザーの直近の対話ログとBot心情評価を取得する（新しい順）。"""
        persona_val = str(persona or "default").strip()
        query = """
            SELECT id, persona, guild_id, channel_id, user_id, user_name,
                   user_text, bot_appraisal, dominant_emotion, emotion_scores_json,
                   affinity, trust, affection, hatred, created_at
            FROM user_relationship_logs
            WHERE persona = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """
        async with self._cursor() as cur:
            await cur.execute(query, (persona_val, int(user_id), max(1, int(limit))))
            rows = await fetch_all_dicts(cur)
            return rows

    async def trim_old_user_relationship_logs(
        self,
        *,
        persona: str,
        user_id: int,
        keep_count: int = 50,
    ) -> int:
        """ユーザーごとの古いログを削除し、指定件数以内に保つ。"""
        if keep_count <= 0:
            return 0
        persona_val = str(persona or "default").strip()
        async with self._cursor(commit=True) as cur:
            await cur.execute(
                """
                DELETE FROM user_relationship_logs
                WHERE persona = ? AND user_id = ?
                  AND id NOT IN (
                      SELECT id FROM user_relationship_logs
                      WHERE persona = ? AND user_id = ?
                      ORDER BY created_at DESC, id DESC
                      LIMIT ?
                  )
                """,
                (persona_val, int(user_id), persona_val, int(user_id), int(keep_count)),
            )
            return int(cur.rowcount or 0)
