"""
[AI Agent Summary]
MemoryStoreのベース設定、DBコネクション管理、スキーマ初期化、マイグレーションを担当するモジュール。
This module handles MemoryStore base configuration, DB connection lease, schema initialization, and migration.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from lib.db_utils import open_cursor

try:
    import sqlite_vec
    _SQLITE_VEC_AVAILABLE = True
except ImportError:
    sqlite_vec = None
    _SQLITE_VEC_AVAILABLE = False

from ..json_compat import dumps as json_dumps, loads as json_loads

log = logging.getLogger(__name__)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _ConnectionLease:
    """Keep existing call sites intact while reusing a single DB connection."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def close(self) -> None:
        return None


class _MemoryStoreBase:
    def __init__(
        self,
        db_path: str,
        *,
        embed_dim: int = 1024,
        use_sqlite_vec: bool = True,
        use_fts: bool = True,
    ) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.embed_dim = int(embed_dim)
        self.use_sqlite_vec = bool(use_sqlite_vec)
        self.use_fts = bool(use_fts)
        self._init_lock = asyncio.Lock()
        self._conn_lock = asyncio.Lock()
        self._initialized = False
        self._vec_enabled = False
        self._fts_enabled = False
        self._conn: aiosqlite.Connection | None = None
        self._conn_lease: _ConnectionLease | None = None

    @property
    def is_vec_enabled(self) -> bool:
        return self._vec_enabled

    @property
    def is_fts_enabled(self) -> bool:
        return self._fts_enabled

    async def decay_and_purge_user_interests(self, *args: Any, **kwargs: Any) -> Any:
        return None

    async def _connect(self) -> _ConnectionLease:
        lease = self._conn_lease
        if lease is not None:
            return lease

        conn = self._conn
        if conn is None or self._conn_lease is None:
            async with self._conn_lock:
                conn = self._conn
                lease = self._conn_lease
                if conn is None:
                    conn = await aiosqlite.connect(self.db_path, timeout=30.0)
                    try:
                        conn.row_factory = aiosqlite.Row
                        await conn.execute("PRAGMA journal_mode=WAL")
                        await conn.execute("PRAGMA synchronous=NORMAL")
                        await conn.execute("PRAGMA foreign_keys=ON")
                        await conn.execute("PRAGMA busy_timeout=30000")
                        await conn.execute("PRAGMA cache_size=-64000")
                        await conn.execute("PRAGMA mmap_size=268435456")

                        if _SQLITE_VEC_AVAILABLE and self.use_sqlite_vec and sqlite_vec is not None:
                            try:
                                await conn.enable_load_extension(True)
                                await conn.load_extension(sqlite_vec.loadable_path())
                                await conn.enable_load_extension(False)
                                self._vec_enabled = True
                            except Exception as e:
                                log.warning("sqlite-vec extension load failed, fallback to python scoring: %r", e)
                                self._vec_enabled = False
                        else:
                            self._vec_enabled = False
                    except Exception:
                        await conn.close()
                        raise
                if self._conn_lease is None:
                    if conn is None:
                        raise RuntimeError("Database connection not established")
                    self._conn_lease = _ConnectionLease(conn)
        assert self._conn_lease is not None
        return self._conn_lease

    async def close(self) -> None:
        async with self._conn_lock:
            conn = self._conn
            self._conn = None
            self._conn_lease = None
            self._vec_enabled = False
        if conn is not None:
            await conn.close()

    @asynccontextmanager
    async def _cursor(self, *, commit: bool = False) -> AsyncIterator[aiosqlite.Cursor]:
        await self._ensure_initialized()
        conn = await self._connect()
        async with open_cursor(conn, commit=commit) as cur:
            yield cur

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return
            await self._init_db()
            self._initialized = True

    async def _ensure_column(self, cur: aiosqlite.Cursor, table: str, column_name: str, column_sql: str) -> None:
        await cur.execute(f"PRAGMA table_info({table})")
        rows = list(await cur.fetchall())
        names = {str(row["name"]) for row in rows}
        if column_name not in names:
            await cur.execute(f"ALTER TABLE {table} ADD COLUMN {column_sql}")

    async def _sync_vec_memories(self, cur: aiosqlite.Cursor) -> None:
        if not self._vec_enabled or sqlite_vec is None:
            return
        try:
            await cur.execute("""
                SELECT id, embedding_json
                FROM memories
                WHERE embedding_json IS NOT NULL
                  AND id NOT IN (SELECT memory_id FROM vec_memories)
            """)
            unindexed = list(await cur.fetchall())
            if unindexed:
                log.info("syncing %d unindexed memories into vec_memories", len(unindexed))
                for row in unindexed:
                    try:
                        vec = json_loads(row["embedding_json"])
                        if isinstance(vec, list) and len(vec) == self.embed_dim:
                            await cur.execute(
                                "INSERT OR REPLACE INTO vec_memories(memory_id, embedding) VALUES (?, ?)",
                                (int(row["id"]), sqlite_vec.serialize_float32(vec)),
                            )
                    except Exception as e:
                        log.debug("failed to sync memory %s into vec_memories: %r", row["id"], e)
        except Exception as e:
            log.warning("vec_memories sync failed: %r", e)

    async def _sync_fts_memories(self, cur: aiosqlite.Cursor) -> None:
        if not self._fts_enabled:
            return
        try:
            await cur.execute("""
                SELECT id, content
                FROM memories
                WHERE id NOT IN (SELECT rowid FROM fts_memories)
            """)
            unindexed = list(await cur.fetchall())
            if unindexed:
                log.info("syncing %d unindexed memories into fts_memories", len(unindexed))
                for row in unindexed:
                    try:
                        content_val = str(row["content"] or "").strip()
                        if content_val:
                            await cur.execute(
                                "INSERT OR REPLACE INTO fts_memories(rowid, content) VALUES (?, ?)",
                                (int(row["id"]), content_val),
                            )
                    except Exception as e:
                        log.debug("failed to sync memory %s into fts_memories: %r", row["id"], e)
        except Exception as e:
            log.warning("fts_memories sync failed: %r", e)

    async def _init_db(self) -> None:
        conn = await self._connect()
        try:
            cur = await conn.cursor()

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                score REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT,
                emotion_tags TEXT
            )
            """)

            await self._ensure_column(cur, "memories", "emotion_tags", "emotion_tags TEXT")
            await self._ensure_column(cur, "memories", "embedding_json", "embedding_json TEXT")

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_persona_guild_user
            ON memories(persona, guild_id, user_id)
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_persona_guild_channel
            ON memories(persona, guild_id, channel_id)
            """)

            if self._vec_enabled:
                await cur.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                    memory_id INTEGER PRIMARY KEY,
                    embedding float[{self.embed_dim}] distance_metric=cosine
                )
                """)
                await self._sync_vec_memories(cur)

            if self.use_fts:
                try:
                    await cur.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories USING fts5(
                        content,
                        tokenize='trigram'
                    )
                    """)
                    self._fts_enabled = True
                    await self._sync_fts_memories(cur)
                except Exception as e:
                    log.warning("FTS5 trigram initialization failed: %r", e)
                    try:
                        await cur.execute("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories USING fts5(
                            content
                        )
                        """)
                        self._fts_enabled = True
                        await self._sync_fts_memories(cur)
                    except Exception as e2:
                        log.warning("FTS5 default initialization failed, disabling FTS: %r", e2)
                        self._fts_enabled = False

            await conn.commit()

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                source_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_utterances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_bot_utterances_persona_scope_created
            ON bot_utterances(persona, guild_id, channel_id, created_at DESC)
            """)

            await cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_summaries_unique
            ON channel_summaries(persona, guild_id, channel_id)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS channel_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER NOT NULL,
                thread_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                participants TEXT NOT NULL,
                turns_json TEXT NOT NULL,
                last_active_ts REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_threads_unique
            ON channel_threads(persona, guild_id, channel_id, thread_key)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS training_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                context_text TEXT,
                accepted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_training_candidates_persona_created
            ON training_candidates(persona, created_at)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                original_user_text TEXT NOT NULL,
                assistant_text TEXT NOT NULL,
                feedback_text TEXT NOT NULL,
                corrected_assistant_text TEXT,
                feedback_type TEXT,
                accepted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_feedback_items_persona_created
            ON feedback_items(persona, created_at)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                subject_kind TEXT NOT NULL DEFAULT 'agent',
                subject_key TEXT NOT NULL DEFAULT 'core',
                concept TEXT NOT NULL,
                belief TEXT NOT NULL,
                importance_score REAL NOT NULL DEFAULT 0.0,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                source_window_start TEXT,
                source_window_end TEXT,
                created_at TEXT NOT NULL,
                last_used_at TEXT
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reflections_persona_scope_importance
            ON reflections(persona, guild_id, channel_id, importance_score DESC, created_at DESC)
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reflections_persona_subject
            ON reflections(persona, subject_kind, subject_key, created_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS memory_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                summary TEXT NOT NULL,
                what TEXT,
                bot_action TEXT,
                user_reaction TEXT,
                emotion_before_json TEXT,
                emotion_after_json TEXT,
                importance REAL NOT NULL DEFAULT 0.5,
                tags_json TEXT,
                created_at TEXT NOT NULL,
                promoted_at TEXT
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_episodes_scope_created
            ON memory_episodes(persona, guild_id, channel_id, user_id, created_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS memory_prospective (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                target_user_id INTEGER,
                kind TEXT NOT NULL DEFAULT 'event_based',
                cue_keywords_json TEXT,
                cue_topic_tag TEXT,
                intent TEXT NOT NULL,
                priority REAL NOT NULL DEFAULT 0.5,
                expires_at TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                triggered_at TEXT
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_prospective_scope_status
            ON memory_prospective(persona, guild_id, channel_id, target_user_id, status, created_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS habit_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                context_key TEXT NOT NULL,
                action_style TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                value REAL NOT NULL DEFAULT 0.5,
                last_reward REAL,
                last_used_at TEXT NOT NULL,
                UNIQUE(persona, context_key, action_style)
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_habit_records_persona_context
            ON habit_records(persona, context_key, last_used_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS user_habit_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                user_id INTEGER NOT NULL,
                tone_casual REAL NOT NULL DEFAULT 0.5,
                response_detail REAL NOT NULL DEFAULT 0.5,
                search_preference REAL NOT NULL DEFAULT 0.5,
                joke_receptivity REAL NOT NULL DEFAULT 0.5,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                last_signals_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_habit_profiles_scope
            ON user_habit_profiles(persona, guild_id, user_id, updated_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS user_interests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER,
                topic TEXT NOT NULL,
                detail TEXT,
                keywords_json TEXT,
                interest_level REAL NOT NULL DEFAULT 0.5,
                mention_count INTEGER NOT NULL DEFAULT 1,
                last_mentioned_at TEXT NOT NULL,
                last_used_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_interests_scope
            ON user_interests(persona, guild_id, channel_id, interest_level DESC, updated_at DESC)
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_interests_user
            ON user_interests(persona, guild_id, user_id, interest_level DESC, updated_at DESC)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                current_name TEXT NOT NULL,
                current_display_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL DEFAULT '[]',
                roles_json TEXT NOT NULL DEFAULT '[]',
                avatar_url TEXT,
                joined_at TEXT,
                created_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(guild_id, user_id)
            )
            """)

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_profiles_guild_user
            ON user_profiles(guild_id, user_id)
            """)

            await cur.execute("""
            CREATE TABLE IF NOT EXISTS user_relationship_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                user_text TEXT NOT NULL,
                bot_appraisal TEXT,
                dominant_emotion TEXT,
                emotion_scores_json TEXT,
                affinity REAL NOT NULL DEFAULT 0.0,
                trust REAL NOT NULL DEFAULT 0.0,
                affection REAL NOT NULL DEFAULT 0.0,
                hatred REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            )
            """)

            await self._ensure_column(cur, "user_relationship_logs", "affection", "affection REAL NOT NULL DEFAULT 0.0")
            await self._ensure_column(cur, "user_relationship_logs", "hatred", "hatred REAL NOT NULL DEFAULT 0.0")

            await cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_relationship_logs_scope
            ON user_relationship_logs(persona, guild_id, user_id, created_at DESC)
            """)

            await conn.commit()
        finally:
            await conn.close()
