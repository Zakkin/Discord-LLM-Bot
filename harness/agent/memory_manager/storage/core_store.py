"""
[AI Agent Summary]
基本記憶の登録、ベクトル検索、FTS5キーワード検索、ハイブリッド検索、時間減衰パージを担当するモジュール。
This module handles core memory addition, vector search, FTS5 keyword search, hybrid search, and temporal decay/purging.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, TYPE_CHECKING

from ..compat import json_dumps, json_loads, utcnow_iso
from .base import _MemoryStoreBase, _SQLITE_VEC_AVAILABLE, sqlite_vec
from lib.db_utils import fetch_all_dicts, fetch_one_dict

if TYPE_CHECKING:
    _MemoryMixinBase = _MemoryStoreBase
else:
    _MemoryMixinBase = object

log = logging.getLogger(__name__)


class _MemoryCoreMixin(_MemoryMixinBase):
    async def add_memory(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        memory_type: str,
        content: str,
        score: float,
        emotion_tags: list[str] | None = None,
        embedding: list[float] | None = None,
        source_kind: str | None = None,
        source_speaker: str | None = None,
        source_quote: str | None = None,
    ) -> int:
        await self._ensure_initialized()
        now = utcnow_iso()

        content = str(content or "").strip()
        if not content:
            raise ValueError("empty memory content")

        memory_type = str(memory_type or "").strip()[:64]
        content = content[:300]
        score = max(0.0, min(1.0, float(score)))
        normalized_emotion_tags = [str(tag).strip() for tag in (emotion_tags or []) if str(tag).strip()]

        norm_source_kind = str(source_kind or "").strip() or None
        if norm_source_kind not in (None, "human", "bot_only", "mixed", "unknown"):
            norm_source_kind = "unknown"
        norm_source_speaker = str(source_speaker or "").strip()[:64] or None
        norm_source_quote = str(source_quote or "").strip()[:120] or None

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                SELECT id, score, emotion_tags, embedding_json, source_kind, source_speaker, source_quote
                FROM memories
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND COALESCE(user_id, -1) = COALESCE(?, -1)
                  AND memory_type = ?
                  AND content = ?
                LIMIT 1
            """, (persona, guild_id, user_id, memory_type, content))
            row = await cur.fetchone()

            if row:
                merged_tags: list[str] = []
                existing_raw = str(row["emotion_tags"] or "").strip()
                if existing_raw:
                    try:
                        parsed = json_loads(existing_raw)
                        if isinstance(parsed, list):
                            merged_tags.extend([str(v) for v in parsed if str(v).strip()])
                    except Exception:
                        pass
                for tag in emotion_tags or []:
                    tag_text = str(tag).strip()
                    if tag_text and tag_text not in merged_tags:
                        merged_tags.append(tag_text)

                embedding_val = row["embedding_json"]
                if embedding and not embedding_val:
                    embedding_val = json_dumps([float(x) for x in embedding])

                existing_kind = str(row["source_kind"] or "").strip()
                updated_kind: str | None
                if existing_kind and existing_kind != "unknown":
                    updated_kind = existing_kind
                    updated_speaker = row["source_speaker"]
                    updated_quote = row["source_quote"]
                else:
                    updated_kind = norm_source_kind or existing_kind or None
                    updated_speaker = norm_source_speaker if norm_source_speaker is not None else row["source_speaker"]
                    updated_quote = norm_source_quote if norm_source_quote is not None else row["source_quote"]

                target_id = int(row["id"])
                await cur.execute("""
                    UPDATE memories
                    SET score = ?,
                        updated_at = ?,
                        emotion_tags = ?,
                        embedding_json = COALESCE(embedding_json, ?),
                        source_kind = ?,
                        source_speaker = ?,
                        source_quote = ?
                    WHERE id = ?
                """, (
                    max(float(score), float(row["score"])),
                    now,
                    json_dumps(merged_tags, ensure_ascii=False) if merged_tags else None,
                    embedding_val,
                    updated_kind,
                    updated_speaker,
                    updated_quote,
                    target_id,
                ))
                if self._vec_enabled and sqlite_vec is not None and embedding and len(embedding) == self.embed_dim:
                    try:
                        await cur.execute(
                            "INSERT OR REPLACE INTO vec_memories(memory_id, embedding) VALUES (?, ?)",
                            (target_id, sqlite_vec.serialize_float32(embedding)),
                        )
                    except Exception as e:
                        log.warning("failed to insert into vec_memories id=%s: %r", target_id, e)
                if self._fts_enabled:
                    try:
                        await cur.execute(
                            "INSERT OR REPLACE INTO fts_memories(rowid, content) VALUES (?, ?)",
                            (target_id, content),
                        )
                    except Exception as e:
                        log.warning("failed to insert into fts_memories id=%s: %r", target_id, e)
                return target_id

            await cur.execute("""
                INSERT INTO memories (
                    persona, guild_id, channel_id, user_id,
                    memory_type, content, score,
                    created_at, updated_at, last_used_at, emotion_tags, embedding_json,
                    source_kind, source_speaker, source_quote
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                persona, guild_id, channel_id, user_id,
                memory_type, content, score,
                now, now, None,
                json_dumps(normalized_emotion_tags, ensure_ascii=False) if normalized_emotion_tags else None,
                json_dumps([float(x) for x in embedding]) if embedding else None,
                norm_source_kind,
                norm_source_speaker,
                norm_source_quote,
            ))
            inserted_id = int(cur.lastrowid or 0)
            if self._vec_enabled and sqlite_vec is not None and embedding and len(embedding) == self.embed_dim:
                try:
                    await cur.execute(
                        "INSERT OR REPLACE INTO vec_memories(memory_id, embedding) VALUES (?, ?)",
                        (inserted_id, sqlite_vec.serialize_float32(embedding)),
                    )
                except Exception as e:
                    log.warning("failed to insert into vec_memories id=%s: %r", inserted_id, e)
            if self._fts_enabled:
                try:
                    await cur.execute(
                        "INSERT OR REPLACE INTO fts_memories(rowid, content) VALUES (?, ?)",
                        (inserted_id, content),
                    )
                except Exception as e:
                    log.warning("failed to insert into fts_memories id=%s: %r", inserted_id, e)
            return inserted_id

    async def search_memories_by_vector(
        self,
        query_embedding: list[float],
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_global: bool = True,
        include_same_channel_users: bool = True,
        limit: int = 5,
        threshold: float = 0.45,
    ) -> list[dict[str, Any]]:
        """sqlite-vec を用いてコサイン類似度上位の記憶を検索する。"""
        await self._ensure_initialized()
        if not self._vec_enabled or sqlite_vec is None or not query_embedding:
            return []

        if len(query_embedding) != self.embed_dim:
            log.warning("query_embedding dim mismatch %d != %d", len(query_embedding), self.embed_dim)
            return []

        async with self._cursor() as cur:
            params: list[Any] = [
                sqlite_vec.serialize_float32(query_embedding),
                limit,
                persona,
                guild_id,
            ]
            where = [
                "v.embedding MATCH ?",
                "k = ?",
                "m.persona = ?",
                "COALESCE(m.guild_id, -1) = COALESCE(?, -1)",
            ]
            if channel_id is not None:
                where.append("(m.channel_id = ? OR m.channel_id IS NULL)")
                params.append(channel_id)
            if user_id is not None:
                if include_global:
                    if include_same_channel_users and channel_id is not None:
                        where.append(
                            "(m.user_id = ? OR m.user_id IS NULL OR m.channel_id = ? "
                            "OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))"
                        )
                        params.extend([user_id, channel_id])
                    else:
                        where.append(
                            "(m.user_id = ? OR m.user_id IS NULL "
                            "OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))"
                        )
                        params.append(user_id)
                else:
                    where.append("m.user_id = ?")
                    params.append(user_id)
            else:
                if include_global:
                    where.append("(m.user_id IS NULL OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))")
                else:
                    where.append("1 = 0")

            query = f"""
                SELECT
                    m.id, m.guild_id, m.channel_id, m.user_id, m.memory_type,
                    m.content, m.score, m.emotion_tags, m.embedding_json,
                    m.source_kind, m.source_speaker, m.source_quote,
                    m.updated_at, m.created_at,
                    v.distance AS _distance
                FROM vec_memories v
                JOIN memories m ON m.id = v.memory_id
                WHERE {' AND '.join(where)}
                ORDER BY v.distance ASC
            """
            await cur.execute(query, tuple(params))
            rows = await fetch_all_dicts(cur)
            results: list[dict[str, Any]] = []
            for item in rows:
                dist = float(item.get("_distance", 1.0) or 1.0)
                sim = max(0.0, min(1.0, 1.0 - dist))
                if sim >= threshold:
                    item["_semantic_score"] = sim
                    item["_semantic_match"] = True
                    results.append(item)
            return results

    async def search_memories_by_keyword(
        self,
        keyword_query: str,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_global: bool = True,
        include_same_channel_users: bool = True,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """SQLite FTS5 全文検索を用いて BM25 順に記憶を検索する。"""
        await self._ensure_initialized()
        clean_query = str(keyword_query or "").strip()
        if not self._fts_enabled or not clean_query:
            return []

        try:
            async with self._cursor() as cur:
                params: list[Any] = [
                    clean_query,
                    persona,
                    guild_id,
                ]
                where = [
                    "fts_memories MATCH ?",
                    "m.persona = ?",
                    "COALESCE(m.guild_id, -1) = COALESCE(?, -1)",
                ]
                if channel_id is not None:
                    where.append("(m.channel_id = ? OR m.channel_id IS NULL)")
                    params.append(channel_id)
                if user_id is not None:
                    if include_global:
                        if include_same_channel_users and channel_id is not None:
                            where.append(
                                "(m.user_id = ? OR m.user_id IS NULL OR m.channel_id = ? "
                                "OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))"
                            )
                            params.extend([user_id, channel_id])
                        else:
                            where.append(
                                "(m.user_id = ? OR m.user_id IS NULL "
                                "OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))"
                            )
                            params.append(user_id)
                    else:
                        where.append("m.user_id = ?")
                        params.append(user_id)
                else:
                    if include_global:
                        where.append("(m.user_id IS NULL OR m.memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))")
                    else:
                        where.append("1 = 0")

                params.append(limit)
                query = f"""
                    SELECT
                        m.id, m.guild_id, m.channel_id, m.user_id, m.memory_type,
                        m.content, m.score, m.emotion_tags, m.embedding_json,
                        m.source_kind, m.source_speaker, m.source_quote,
                        m.updated_at, m.created_at,
                        f.rank AS _fts_rank
                    FROM fts_memories f
                    JOIN memories m ON m.id = f.rowid
                    WHERE {' AND '.join(where)}
                    ORDER BY f.rank ASC
                    LIMIT ?
                """
                await cur.execute(query, tuple(params))
                rows = await fetch_all_dicts(cur)
                for item in rows:
                    item["_keyword_match"] = True
                return rows
        except Exception as e:
            log.debug("search_memories_by_keyword query failed for %r: %r", clean_query, e)
            return []

    async def search_memories_hybrid(
        self,
        query_embedding: list[float],
        keyword_query: str | None = None,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_global: bool = True,
        include_same_channel_users: bool = True,
        limit: int = 5,
        threshold: float = 0.45,
        dense_weight: float = 1.0,
        sparse_weight: float = 1.0,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        """Dense (ベクトル) と Sparse (FTS5 BM25) を並行実行し、RRF で統合する。"""
        await self._ensure_initialized()
        candidate_limit = max(limit * 2, 10)

        dense_coro = self.search_memories_by_vector(
            query_embedding,
            persona=persona,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            include_global=include_global,
            include_same_channel_users=include_same_channel_users,
            limit=candidate_limit,
            threshold=threshold,
        )
        sparse_coro = (
            self.search_memories_by_keyword(
                keyword_query,
                persona=persona,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                include_global=include_global,
                include_same_channel_users=include_same_channel_users,
                limit=candidate_limit,
            )
            if keyword_query and self._fts_enabled
            else asyncio.sleep(0, result=[])
        )

        dense_results, sparse_results = await asyncio.gather(dense_coro, sparse_coro)

        if not dense_results and not sparse_results:
            return []
        if not sparse_results:
            return dense_results[:limit]
        if not dense_results:
            for item in sparse_results:
                item["_semantic_match"] = True
                item["_semantic_score"] = float(item.get("score", 0.8) or 0.8)
            return sparse_results[:limit]

        rrf_scores: dict[int, float] = {}
        merged_items: dict[int, dict[str, Any]] = {}

        for rank_idx, item in enumerate(dense_results):
            mid = int(item["id"])
            merged_items[mid] = dict(item)
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + (dense_weight / (rrf_k + rank_idx + 1))

        for rank_idx, item in enumerate(sparse_results):
            mid = int(item["id"])
            if mid not in merged_items:
                merged_items[mid] = dict(item)
            else:
                merged_items[mid]["_keyword_match"] = True
            rrf_scores[mid] = rrf_scores.get(mid, 0.0) + (sparse_weight / (rrf_k + rank_idx + 1))

        sorted_mids = sorted(rrf_scores.keys(), key=lambda m: rrf_scores[m], reverse=True)
        final_results: list[dict[str, Any]] = []
        for mid in sorted_mids[:limit]:
            item = merged_items[mid]
            item["_rrf_score"] = rrf_scores[mid]
            item["_semantic_match"] = True
            if "_semantic_score" not in item:
                item["_semantic_score"] = float(item.get("score", 0.8) or 0.8)
            final_results.append(item)

        return final_results

    async def _get_recent_memories_by_scope(
        self,
        *,
        persona: str,
        guild_id: int | None,
        scope_col: str,
        scope_val: int | None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        sql = f"""
            SELECT *
            FROM memories
            WHERE persona = ?
              AND COALESCE(guild_id, -1) = COALESCE(?, -1)
              AND COALESCE({scope_col}, -1) = COALESCE(?, -1)
              AND score >= ?
            ORDER BY score DESC, updated_at DESC
            LIMIT ?
        """
        async with self._cursor() as cur:
            await cur.execute(sql, (persona, guild_id, scope_val, float(min_score), int(limit)))
            return await fetch_all_dicts(cur)

    async def get_recent_user_memories(
        self,
        *,
        persona: str,
        guild_id: int | None,
        user_id: int | None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        return await self._get_recent_memories_by_scope(
            persona=persona,
            guild_id=guild_id,
            scope_col="user_id",
            scope_val=user_id,
            limit=limit,
            min_score=min_score,
        )

    async def get_recent_channel_memories(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        limit: int = 5,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        return await self._get_recent_memories_by_scope(
            persona=persona,
            guild_id=guild_id,
            scope_col="channel_id",
            scope_val=channel_id,
            limit=limit,
            min_score=min_score,
        )

    async def get_random_memories(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        limit: int = 3,
        min_score: float = 0.0,
        exclude_memory_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [persona, guild_id, float(min_score)]
        where = [
            "persona = ?",
            "COALESCE(guild_id, -1) = COALESCE(?, -1)",
            "score >= ?",
            "TRIM(COALESCE(content, '')) <> ''",
        ]
        if channel_id is not None:
            where.append("(COALESCE(channel_id, -1) = COALESCE(?, -1) OR COALESCE(channel_id, -1) = -1)")
            params.append(channel_id)
        if exclude_memory_types:
            placeholders = ", ".join("?" for _ in exclude_memory_types)
            where.append(f"memory_type NOT IN ({placeholders})")
            params.extend([str(v) for v in exclude_memory_types])

        params.append(int(limit))
        async with self._cursor() as cur:
            await cur.execute(
                f"""
                SELECT *
                FROM memories
                WHERE {' AND '.join(where)}
                ORDER BY RANDOM()
                LIMIT ?
                """,
                tuple(params),
            )
            return await fetch_all_dicts(cur)

    async def get_memories_with_embeddings(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_global: bool = True,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [persona, guild_id]
        where = [
            "persona = ?",
            "COALESCE(guild_id, -1) = COALESCE(?, -1)",
            "embedding_json IS NOT NULL",
        ]
        if channel_id is not None:
            where.append("(channel_id = ? OR channel_id IS NULL)")
            params.append(channel_id)
        if user_id is not None:
            if include_global:
                where.append("(user_id = ? OR user_id IS NULL OR memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))")
                params.append(user_id)
            else:
                where.append("user_id = ?")
                params.append(user_id)
        else:
            if include_global:
                where.append("(user_id IS NULL OR memory_type IN ('fact', 'setting', 'theme', 'conversation_theme'))")
            else:
                where.append("1 = 0")

        async with self._cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, guild_id, channel_id, user_id, memory_type, content, score, emotion_tags, embedding_json, source_kind, source_speaker, source_quote, updated_at, created_at
                FROM memories
                WHERE {' AND '.join(where)}
                """,
                tuple(params),
            )
            return await fetch_all_dicts(cur)

    async def touch_memory(self, memory_id: int) -> None:
        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                UPDATE memories
                SET last_used_at = ?
                WHERE id = ?
            """, (utcnow_iso(), int(memory_id)))

    async def get_memories_in_window(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None = None,
        start_at: str,
        end_at: str,
        limit: int = 200,
        exclude_memory_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = [persona, guild_id, start_at, end_at]
        where = [
            "persona = ?",
            "COALESCE(guild_id, -1) = COALESCE(?, -1)",
            "created_at >= ?",
            "created_at <= ?",
            "TRIM(COALESCE(content, '')) <> ''",
        ]
        if channel_id is not None:
            where.append("(COALESCE(channel_id, -1) = COALESCE(?, -1) OR COALESCE(channel_id, -1) = -1)")
            params.append(channel_id)
        if exclude_memory_types:
            placeholders = ", ".join("?" for _ in exclude_memory_types)
            where.append(f"memory_type NOT IN ({placeholders})")
            params.extend([str(v) for v in exclude_memory_types])
        params.append(int(limit))
        async with self._cursor() as cur:
            await cur.execute(
                f"""
                SELECT *
                FROM memories
                WHERE {' AND '.join(where)}
                ORDER BY score DESC, updated_at DESC
                LIMIT ?
                """,
                tuple(params),
            )
            return await fetch_all_dicts(cur)

    async def decay_and_purge_memories(
        self,
        *,
        persona: str,
        decay_ratio: float = 0.95,
        purge_threshold: float = 0.20,
        purge_days: float = 5.0,
    ) -> None:
        now = datetime.now(timezone.utc)
        purge_limit_dt = now - timedelta(days=purge_days)
        purge_limit_iso = purge_limit_dt.isoformat()

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                UPDATE memories
                SET score = score * ?
                WHERE persona = ?
            """, (max(0.0, min(1.0, float(decay_ratio))), persona))

            await cur.execute("""
                DELETE FROM memories
                WHERE persona = ?
                  AND score < ?
                  AND updated_at < ?
                  AND (last_used_at IS NULL OR last_used_at < ?)
            """, (persona, float(purge_threshold), purge_limit_iso, purge_limit_iso))

        with contextlib.suppress(Exception):
            await self.decay_and_purge_user_interests(
                persona=persona,
                decay_ratio=decay_ratio,
                purge_threshold=max(0.05, purge_threshold * 0.75),
                purge_days=max(purge_days * 2, 14.0),
            )

    async def delete_user_data(
        self,
        *,
        persona: str,
        guild_id: int | None,
        user_id: int,
    ) -> dict[str, int]:
        """対象ユーザーに紐づくDB内全データを1トランザクションで物理削除し、テーブル名->削除件数の辞書を返す。
        対象: memories (※対応する vec_memories, fts_memories も必ず同一トランザクションで連動削除),
        memory_episodes, memory_prospective, user_habit_records, user_interests,
        reflections, user_relationship_logs, feedback_items, training_candidates。
        """
        persona_str = str(persona or "").strip() or "default"
        uid = int(user_id)

        counts: dict[str, int] = {
            "memories": 0,
            "memory_episodes": 0,
            "memory_prospective": 0,
            "user_habit_records": 0,
            "user_interests": 0,
            "reflections": 0,
            "user_relationship_logs": 0,
            "feedback_items": 0,
            "training_candidates": 0,
        }

        async with self._cursor(commit=True) as cur:
            # 1. memories のID一覧を取得
            await cur.execute(
                "SELECT id FROM memories WHERE persona = ? AND user_id = ?",
                (persona_str, uid),
            )
            mem_rows = await cur.fetchall()
            mem_ids = [int(r[0]) for r in mem_rows]

            if mem_ids:
                placeholders = ",".join("?" for _ in mem_ids)
                # BAN-183: FTS & vec 連動削除
                try:
                    await cur.execute(f"DELETE FROM fts_memories WHERE rowid IN ({placeholders})", mem_ids)
                except Exception as e:
                    log.debug("fts_memories delete skipped or failed: %r", e)
                try:
                    await cur.execute(f"DELETE FROM vec_memories WHERE memory_id IN ({placeholders})", mem_ids)
                except Exception as e:
                    log.debug("vec_memories delete skipped or failed: %r", e)
                del_cur = await cur.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", mem_ids)
                counts["memories"] = int(del_cur.rowcount or 0)

            # 2. 関連テーブルの削除
            tables = [
                ("memory_episodes", "user_id"),
                ("memory_prospective", "target_user_id"),
                ("user_relationship_logs", "user_id"),
                ("user_interests", "user_id"),
                ("feedback_items", "user_id"),
                ("training_candidates", "user_id"),
            ]
            for tbl, uid_col in tables:
                try:
                    c = await cur.execute(
                        f"DELETE FROM {tbl} WHERE persona = ? AND {uid_col} = ?",
                        (persona_str, uid),
                    )
                    counts[tbl] = int(c.rowcount or 0)
                except Exception:
                    pass

            # 3. reflections の削除 (subject_kind='user', subject_key=str(user_id))
            try:
                c = await cur.execute(
                    "DELETE FROM reflections WHERE persona = ? AND subject_kind = 'user' AND subject_key = ?",
                    (persona_str, str(uid)),
                )
                counts["reflections"] = int(c.rowcount or 0)
            except Exception:
                pass

            # 4. user_habit_records / user_habit_profiles の削除
            habit_count = 0
            try:
                c = await cur.execute(
                    "DELETE FROM user_habit_profiles WHERE persona = ? AND user_id = ?",
                    (persona_str, uid),
                )
                habit_count += int(c.rowcount or 0)
                counts["user_habit_profiles"] = int(c.rowcount or 0)
            except Exception:
                pass
            try:
                c = await cur.execute(
                    "DELETE FROM user_habit_records WHERE persona = ? AND user_id = ?",
                    (persona_str, uid),
                )
                habit_count += int(c.rowcount or 0)
            except Exception:
                pass
            counts["user_habit_records"] = habit_count

        return counts

    async def get_user_memory_overview(
        self,
        *,
        persona: str,
        guild_id: int | None,
        user_id: int,
        limit: int = 10,
    ) -> dict[str, Any]:
        """ユーザーのプロファイル要約、最近の記憶一覧（id, text, created_at）、総件数を返す。"""
        persona_str = str(persona or "").strip() or "default"
        uid = int(user_id)
        limit_val = max(1, int(limit))

        async with self._cursor() as cur:
            # 1. memories 総件数の取得
            await cur.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE persona = ? AND user_id = ?",
                (persona_str, uid),
            )
            count_row = await cur.fetchone()
            total_count = int(count_row[0] if count_row else 0)

            # 2. 直近 memories 一覧の取得 (id, text, created_at)
            await cur.execute(
                """
                SELECT id, content, created_at
                FROM memories
                WHERE persona = ? AND user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (persona_str, uid, limit_val),
            )
            rows = await fetch_all_dicts(cur)
            recent_memories = [
                {
                    "id": int(r["id"]),
                    "text": str(r.get("content") or ""),
                    "content": str(r.get("content") or ""),
                    "created_at": str(r.get("created_at") or ""),
                }
                for r in rows
            ]

        # 3. ユーザープロファイル要約・情報
        profile = None
        if hasattr(self, "get_user_profile"):
            gid = int(guild_id) if guild_id is not None else 0
            profile = await self.get_user_profile(guild_id=gid, user_id=uid)
            if not profile and gid != 0:
                profile = await self.get_user_profile(guild_id=0, user_id=uid)

        habit_profile = None
        if hasattr(self, "get_user_habit_profile"):
            habit_profile = await self.get_user_habit_profile(
                persona=persona_str,
                guild_id=guild_id,
                user_id=uid,
            )

        return {
            "persona": persona_str,
            "guild_id": guild_id,
            "user_id": uid,
            "total_count": total_count,
            "recent_memories": recent_memories,
            "profile": profile,
            "habit_profile": habit_profile,
        }
