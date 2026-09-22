"""
[AI Agent Summary]
ユーザー関心事（user_interests）、ユーザープロファイル・名前変更履歴（user_profiles, aliases）を担当するモジュール。
This module handles user interests tracking, decay, and user profiles/alias management.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, TYPE_CHECKING

from ..json_compat import dumps as json_dumps, loads as json_loads
from .base import _MemoryStoreBase, utcnow_iso
from lib.db_utils import fetch_all_dicts, fetch_one_dict

if TYPE_CHECKING:
    _MemoryMixinBase = _MemoryStoreBase
else:
    _MemoryMixinBase = object

log = logging.getLogger(__name__)


class _MemoryProfilesMixin(_MemoryMixinBase):
    async def upsert_user_interest(
        self,
        *,
        persona: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int | None,
        topic: str,
        detail: str | None = None,
        keywords: list[str] | None = None,
        interest_level: float = 0.5,
    ) -> int:
        persona = str(persona or "default").strip()
        topic = str(topic or "").strip()
        if not topic:
            raise ValueError("topic is required for user interest")

        detail_val = str(detail or "").strip() or None
        keywords_json = json_dumps(keywords) if keywords else None
        level = max(0.0, min(1.0, float(interest_level)))
        now_iso = utcnow_iso()

        async with self._cursor(commit=True) as cur:
            # 同一ユーザー・ペルソナ・ギルド・類似トピックの既存レコードを検索
            query = """
                SELECT id, detail, keywords_json, interest_level, mention_count
                FROM user_interests
                WHERE persona = ?
                  AND COALESCE(guild_id, -1) = COALESCE(?, -1)
                  AND COALESCE(user_id, -1) = COALESCE(?, -1)
                  AND LOWER(TRIM(topic)) = LOWER(?)
                LIMIT 1
            """
            await cur.execute(query, (persona, guild_id, user_id, topic))
            row = await cur.fetchone()

            if row:
                interest_id = int(row["id"])
                existing_level = float(row["interest_level"] or 0.0)
                mention_count = int(row["mention_count"] or 1) + 1
                # 関心度は最新と既存の高い方をベースに少し加算
                new_level = min(1.0, max(existing_level, level) + 0.05)
                merged_detail = detail_val or row["detail"]

                merged_kw = set(keywords or [])
                if row["keywords_json"]:
                    try:
                        merged_kw.update(json_loads(row["keywords_json"]) or [])
                    except Exception:
                        pass
                merged_kw_json = json_dumps(sorted(list(merged_kw))) if merged_kw else keywords_json

                await cur.execute("""
                    UPDATE user_interests
                    SET detail = ?,
                        keywords_json = ?,
                        interest_level = ?,
                        mention_count = ?,
                        last_mentioned_at = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (merged_detail, merged_kw_json, new_level, mention_count, now_iso, now_iso, interest_id))
                return interest_id
            else:
                await cur.execute("""
                    INSERT INTO user_interests (
                        persona, guild_id, channel_id, user_id,
                        topic, detail, keywords_json, interest_level,
                        mention_count, last_mentioned_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """, (
                    persona, guild_id, channel_id, user_id,
                    topic, detail_val, keywords_json, level,
                    now_iso, now_iso, now_iso
                ))
                return int(cur.lastrowid or 0)

    async def list_user_interests(
        self,
        *,
        persona: str,
        guild_id: int | None = None,
        channel_id: int | None = None,
        user_id: int | None = None,
        limit: int = 10,
        min_interest_level: float = 0.0,
    ) -> list[dict[str, Any]]:
        persona = str(persona or "default").strip()
        params: list[Any] = [persona]
        where = ["persona = ?"]

        if guild_id is not None:
            where.append("(guild_id IS NULL OR guild_id = ?)")
            params.append(guild_id)
        if channel_id is not None:
            where.append("(channel_id IS NULL OR channel_id = ?)")
            params.append(channel_id)
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)

        where.append("interest_level >= ?")
        params.append(float(min_interest_level))

        params.append(max(1, int(limit)))
        sql = f"""
            SELECT *
            FROM user_interests
            WHERE {' AND '.join(where)}
            ORDER BY interest_level DESC, updated_at DESC
            LIMIT ?
        """
        async with self._cursor() as cur:
            await cur.execute(sql, tuple(params))
            return await fetch_all_dicts(cur)

    async def get_user_interest(self, interest_id: int) -> dict[str, Any] | None:
        async with self._cursor() as cur:
            await cur.execute("SELECT * FROM user_interests WHERE id = ?", (int(interest_id),))
            return await fetch_one_dict(cur)

    async def touch_user_interest(self, interest_id: int) -> None:
        now_iso = utcnow_iso()
        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                UPDATE user_interests
                SET last_used_at = ?, updated_at = ?
                WHERE id = ?
            """, (now_iso, now_iso, int(interest_id)))

    async def decay_and_purge_user_interests(
        self,
        *,
        persona: str,
        decay_ratio: float = 0.95,
        purge_threshold: float = 0.15,
        purge_days: float = 14.0,
    ) -> None:
        persona = str(persona or "default").strip()
        now = datetime.now(timezone.utc)
        purge_limit_iso = (now - timedelta(days=purge_days)).isoformat()

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                UPDATE user_interests
                SET interest_level = interest_level * ?
                WHERE persona = ?
            """, (max(0.0, min(1.0, float(decay_ratio))), persona))

            await cur.execute("""
                DELETE FROM user_interests
                WHERE persona = ?
                  AND interest_level < ?
                  AND updated_at < ?
            """, (persona, float(purge_threshold), purge_limit_iso))

    async def upsert_user_profile(
        self,
        *,
        guild_id: int,
        user_id: int,
        current_name: str,
        current_display_name: str,
        new_alias: str | None = None,
        roles: list[str] | None = None,
        avatar_url: str | None = None,
        joined_at: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_initialized()
        now_iso = utcnow_iso()
        name_clean = str(current_name or "").strip()
        display_clean = str(current_display_name or "").strip() or name_clean
        if not name_clean:
            name_clean = display_clean or f"user_{user_id}"

        async with self._cursor(commit=True) as cur:
            await cur.execute("""
                SELECT * FROM user_profiles
                WHERE guild_id = ? AND user_id = ?
            """, (int(guild_id), int(user_id)))
            row = await cur.fetchone()

            aliases: list[str] = []
            roles_list: list[str] = [str(r).strip() for r in (roles or []) if str(r).strip()]
            existing_avatar = avatar_url
            existing_joined_at = joined_at
            existing_created_at = created_at

            if row:
                raw_aliases = str(row["aliases_json"] or "").strip()
                if raw_aliases:
                    try:
                        parsed = json_loads(raw_aliases)
                        if isinstance(parsed, list):
                            aliases = [str(a).strip() for a in parsed if str(a).strip()]
                    except Exception:
                        aliases = []

                old_name = str(row["current_name"] or "").strip()
                old_display = str(row["current_display_name"] or "").strip()

                # 名前が変更されていた場合、過去の名前をエイリアスに追加
                for old_val in (old_display, old_name):
                    if old_val and old_val != display_clean and old_val != name_clean and old_val not in aliases:
                        aliases.append(old_val)

                if roles is None:
                    raw_roles = str(row["roles_json"] or "").strip()
                    if raw_roles:
                        try:
                            parsed_roles = json_loads(raw_roles)
                            if isinstance(parsed_roles, list):
                                roles_list = [str(r).strip() for r in parsed_roles if str(r).strip()]
                        except Exception:
                            pass

                if existing_avatar is None:
                    existing_avatar = row["avatar_url"]
                if existing_joined_at is None:
                    existing_joined_at = row["joined_at"]
                if existing_created_at is None:
                    existing_created_at = row["created_at"]

            if new_alias:
                alias_clean = str(new_alias).strip()
                if alias_clean and alias_clean != display_clean and alias_clean != name_clean and alias_clean not in aliases:
                    aliases.append(alias_clean)

            aliases_json = json_dumps(aliases, ensure_ascii=False)
            roles_json = json_dumps(roles_list, ensure_ascii=False)

            if row:
                await cur.execute("""
                    UPDATE user_profiles
                    SET current_name = ?,
                        current_display_name = ?,
                        aliases_json = ?,
                        roles_json = ?,
                        avatar_url = ?,
                        joined_at = ?,
                        created_at = ?,
                        updated_at = ?
                    WHERE guild_id = ? AND user_id = ?
                """, (
                    name_clean,
                    display_clean,
                    aliases_json,
                    roles_json,
                    existing_avatar,
                    existing_joined_at,
                    existing_created_at,
                    now_iso,
                    int(guild_id),
                    int(user_id),
                ))
            else:
                await cur.execute("""
                    INSERT INTO user_profiles (
                        guild_id, user_id, current_name, current_display_name,
                        aliases_json, roles_json, avatar_url, joined_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    int(guild_id),
                    int(user_id),
                    name_clean,
                    display_clean,
                    aliases_json,
                    roles_json,
                    existing_avatar,
                    existing_joined_at,
                    existing_created_at,
                    now_iso,
                ))

            return {
                "guild_id": int(guild_id),
                "user_id": int(user_id),
                "current_name": name_clean,
                "current_display_name": display_clean,
                "aliases": aliases,
                "roles": roles_list,
                "avatar_url": existing_avatar,
                "joined_at": existing_joined_at,
                "created_at": existing_created_at,
                "updated_at": now_iso,
            }

    async def get_user_profile(
        self,
        *,
        guild_id: int,
        user_id: int,
    ) -> dict[str, Any] | None:
        async with self._cursor() as cur:
            await cur.execute("""
                SELECT * FROM user_profiles
                WHERE guild_id = ? AND user_id = ?
            """, (int(guild_id), int(user_id)))
            res = await fetch_one_dict(cur)
            if not res:
                return None
            try:
                res["aliases"] = json_loads(res.get("aliases_json") or "[]")
            except Exception:
                res["aliases"] = []
            try:
                res["roles"] = json_loads(res.get("roles_json") or "[]")
            except Exception:
                res["roles"] = []
            return res

    async def list_user_profiles_for_guild(
        self,
        *,
        guild_id: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        async with self._cursor() as cur:
            await cur.execute("""
                SELECT * FROM user_profiles
                WHERE guild_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
            """, (int(guild_id), max(1, int(limit))))
            results = await fetch_all_dicts(cur)
            for item in results:
                try:
                    item["aliases"] = json_loads(item.get("aliases_json") or "[]")
                except Exception:
                    item["aliases"] = []
                try:
                    item["roles"] = json_loads(item.get("roles_json") or "[]")
                except Exception:
                    item["roles"] = []
            return results

    async def find_users_by_name_or_alias(
        self,
        *,
        guild_id: int,
        query_name: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """現在名または過去のエイリアスからユーザーを検索する。"""
        await self._ensure_initialized()
        cleaned_query = str(query_name or "").strip().lower()
        if not cleaned_query:
            return []

        profiles = await self.list_user_profiles_for_guild(guild_id=guild_id, limit=500)
        matched: list[dict[str, Any]] = []
        for prof in profiles:
            cur_disp = str(prof.get("current_display_name") or "").strip().lower()
            cur_name = str(prof.get("current_name") or "").strip().lower()
            aliases = [str(a).strip() for a in (prof.get("aliases") or []) if str(a).strip()]

            # 1. 現在の表示名と完全/部分一致
            if cleaned_query == cur_disp:
                matched.append({**prof, "match_type": "exact_display_name", "matched_term": prof.get("current_display_name")})
                continue
            if cleaned_query == cur_name:
                matched.append({**prof, "match_type": "exact_name", "matched_term": prof.get("current_name")})
                continue

            # 2. 過去のエイリアスと一致
            alias_match = None
            for alias in aliases:
                if cleaned_query == alias.lower():
                    alias_match = alias
                    break
            if alias_match:
                matched.append({**prof, "match_type": "exact_alias", "matched_term": alias_match})
                continue

            # 3. 部分一致 (3文字以上の場合のみ)
            if len(cleaned_query) >= 3:
                if cleaned_query in cur_disp:
                    matched.append({**prof, "match_type": "partial_display_name", "matched_term": prof.get("current_display_name")})
                    continue
                for alias in aliases:
                    if cleaned_query in alias.lower():
                        matched.append({**prof, "match_type": "partial_alias", "matched_term": alias})
                        break

        return matched[:max(1, int(limit))]
