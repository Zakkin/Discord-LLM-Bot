"""
[AI Agent Summary]
会話履歴の振り返り（Reflection / Deep Reflection）、信念・コア方針の抽出と保存を担当するMixin。
This module handles periodic and deep reflection over conversation history, extracting and storing core beliefs and user-specific relational policies.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import time
from typing import Any

import discord

from ...common.config_helpers import (
    cfg,
    cfg_bool,
    cfg_managed_channel_ids,
    cfg_persona_namespace,
)
from ...common.discord_helpers import author_id, channel_id
from ...common.emotion_helpers import (
    build_memory_emotion_tags,
    format_emotion_tags_ja,
)
from ...common.ollama_helpers import call_ollama_json
from harness.agent.memory_manager.reflection import (
    DEEP_REFLECTION_SYSTEM_PROMPT,
    DEFAULT_REFLECTION_SYSTEM_PROMPT,
    build_deep_reflection_prompt,
    build_reflection_memory_lines,
    build_reflection_prompt,
    is_reflection_belief_valid,
    merge_reflection_rows,
    normalize_reflection_belief,
)
import logging
from typing import TYPE_CHECKING

log = logging.getLogger("ollama_bot.ollama_chat_.reflection")

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _AgentReflectionBase = OllamaChatProtocol
else:
    _AgentReflectionBase = object


class _AgentReflectionMixin(_AgentReflectionBase):
    def _get_reflection_system_prompt(self) -> str:
        prompt = str(cfg("REFLECTION_SYSTEM_PROMPT", DEFAULT_REFLECTION_SYSTEM_PROMPT) or "").strip()
        return prompt or DEFAULT_REFLECTION_SYSTEM_PROMPT

    async def _get_core_beliefs_for_prompt(self, message: discord.Message) -> list[str]:
        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id = getattr(message.guild, "id", None)
        cid = channel_id(message)
        max_items = int(cfg("REFLECTION_PROMPT_MAX_BELIEFS", 3) or 3)
        user_limit = max_items if max_items <= 1 else max_items - 1
        min_importance = float(cfg("REFLECTION_PROMPT_MIN_IMPORTANCE", 0.45) or 0.45)
        rows_by_scope: list[list[dict[str, Any]]] = []

        uid = author_id(message)
        author_name = getattr(getattr(message, "author", None), "name", "")
        author_disp = getattr(getattr(message, "author", None), "display_name", author_name) or author_name

        if uid is not None:
            user_rows = await self.memory_store.list_reflections(
                persona=persona,
                guild_id=guild_id,
                channel_id=cid,
                subject_kind="user",
                subject_key=str(uid),
                limit=user_limit,
                min_importance=min_importance,
            )
            if user_rows and (author_disp or uid):
                aliases: list[str] = []
                try:
                    from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
                    aliases = await get_user_aliases_from_store(
                        uid,
                        guild=message.guild,
                        memory_store=self.memory_store,
                    )
                except Exception:
                    pass
                for r in user_rows:
                    raw_b = str(r.get("belief") or "")
                    if raw_b:
                        r["belief"] = normalize_reflection_belief(
                            raw_b,
                            target_user_name=author_disp,
                            target_user_id=uid,
                            aliases=aliases,
                        )
            rows_by_scope.append(user_rows)

        rows_by_scope.append(await self.memory_store.list_reflections(
            persona=persona,
            guild_id=guild_id,
            channel_id=cid,
            subject_kind="agent",
            subject_key="core",
            limit=max_items,
            min_importance=min_importance,
        ))
        beliefs, touched_ids = merge_reflection_rows(rows_by_scope, limit=max_items)
        for rid in touched_ids:
            with contextlib.suppress(Exception):
                await self.memory_store.touch_reflection(rid)
        return beliefs[:max_items]

    async def _should_run_reflection_now(self) -> bool:
        if not cfg_bool("REFLECTION_ENABLED", True):
            return False
        idle_sec = max(time.time() - float(self._last_managed_human_message_ts or 0.0), 0.0)
        if idle_sec < float(cfg("REFLECTION_MIN_IDLE_SEC", 60 * 60) or 3600):
            return False
        return True

    async def _run_deep_reflection_once(
        self,
        *,
        channel_id: int | None,
        emotion_scores: dict[str, float],
        emotion_reason: str,
    ) -> None:
        if not cfg_bool("REFLECTION_ENABLED", True):
            return
        if not channel_id:
            return

        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id = self.bot.guilds[0].id if self.bot.guilds else None

        now_dt = datetime.now(timezone.utc)
        window_end = now_dt
        window_start = now_dt - timedelta(hours=1)

        memories = await self.memory_store.get_memories_in_window(
            persona=persona,
            guild_id=guild_id,
            channel_id=channel_id,
            start_at=window_start.isoformat(),
            end_at=window_end.isoformat(),
            limit=15,
            exclude_memory_types=["agent_thought", "agent_knowledge"],
        )
        if not memories:
            return

        memory_lines = build_reflection_memory_lines(memories, source_limit=15)
        if not memory_lines:
            return

        emotion_tags = build_memory_emotion_tags(emotion_scores, max_tags=2, min_score=0.34)
        emotion_text = format_emotion_tags_ja(emotion_tags) or "特になし"

        emotion_reason_lines = [
            f"感じた大きな感情: {emotion_text}",
            f"その理由: {emotion_reason}",
        ]

        prompt = build_deep_reflection_prompt(
            memory_lines=memory_lines,
            emotion_reason_lines=emotion_reason_lines,
        )

        schema = {
            "type": "object",
            "properties": {
                "what_happened": {"type": "string"},
                "interpretation": {"type": "string"},
                "relationship_change": {"type": "string"},
                "future_policy": {"type": "string"},
            },
            "required": ["what_happened", "interpretation", "relationship_change", "future_policy"],
            "additionalProperties": False,
        }

        try:
            result = await call_ollama_json(
                prompt,
                system_prompt=DEEP_REFLECTION_SYSTEM_PROMPT,
                schema=schema,
                think=False,
                model=str(cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MODEL", "")) or ""),
                temperature=0.3,
                timeout_sec=120.0,
                retries=max(0, int(cfg("REFLECTION_RETRIES", 0) or 0)),
            )
            policy = str((result or {}).get("future_policy", "")).strip()
            if policy:
                await self.memory_store.add_reflection(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    subject_kind="agent",
                    subject_key="core",
                    concept="直近の方針(Deep Reflection)",
                    belief=policy[:220],
                    importance_score=0.85,
                    evidence_count=1,
                    source_window_start=window_start.isoformat(),
                    source_window_end=window_end.isoformat(),
                )
        except Exception as e:
            log.exception("deep reflection once failed: %s", e)

    async def _run_reflection_once(self) -> None:
        if not await self._should_run_reflection_now():
            return

        persona = cfg_persona_namespace(custom_cfg=cfg)
        guild_id = self.bot.guilds[0].id if self.bot.guilds else None
        channel_ids = sorted(cfg_managed_channel_ids())
        if not channel_ids:
            return

        now_dt = datetime.now(timezone.utc)
        window_end = now_dt
        window_start = now_dt - timedelta(hours=float(cfg("REFLECTION_WINDOW_HOURS", 24) or 24))

        source_limit = int(cfg("REFLECTION_SOURCE_MEMORY_LIMIT", 80) or 80)
        min_memory_count = int(cfg("REFLECTION_MIN_MEMORY_COUNT", 8) or 8)
        user_min_memory_count = max(2, int(cfg("REFLECTION_USER_MIN_MEMORY_COUNT", 4) or 4))
        max_user_targets = max(0, min(int(cfg("REFLECTION_MAX_USER_TARGETS", 3) or 3), 5))
        max_beliefs = max(1, min(int(cfg("REFLECTION_PROMPT_MAX_BELIEFS", 3) or 3), int(cfg("REFLECTION_MAX_SAVE", 3) or 3), 5))
        reflection_retries = max(0, int(cfg("REFLECTION_RETRIES", 0) or 0))
        reflection_timeout_sec = float(cfg("REFLECTION_TIMEOUT_SEC", 120.0) or 120.0)
        reflection_num_predict = max(128, int(cfg("REFLECTION_NUM_PREDICT", 768) or 768))
        reflection_model = str(
            cfg("REFLECTION_MODEL", "")
            or cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MODEL", ""))
            or ""
        ).strip()
        if not reflection_model:
            log.warning("reflection skipped: no model configured")
            return

        schema = {
            "type": "object",
            "properties": {
                "beliefs": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": max_beliefs,
                    "items": {
                        "type": "object",
                        "properties": {
                            "concept": {"type": "string"},
                            "belief": {"type": "string"},
                            "importance_score": {"type": "number"},
                            "evidence_count": {"type": "integer"},
                        },
                        "required": ["concept", "belief", "importance_score", "evidence_count"],
                        "additionalProperties": False,
                    }
                }
            },
            "required": ["beliefs"],
            "additionalProperties": False,
        }

        for reflection_channel_id in channel_ids:
            memories = await self.memory_store.get_memories_in_window(
                persona=persona,
                guild_id=guild_id,
                channel_id=reflection_channel_id,
                start_at=window_start.isoformat(),
                end_at=window_end.isoformat(),
                limit=source_limit,
                exclude_memory_types=["agent_thought", "agent_knowledge"],
            )
            if len(memories) < min_memory_count:
                continue

            guild = self.bot.get_guild(guild_id) if (guild_id and hasattr(self.bot, "get_guild")) else None
            user_label_map: dict[int, str] = {}
            if guild is not None:
                for item in memories:
                    raw_uid = item.get("user_id")
                    if raw_uid is not None:
                        try:
                            uid_int = int(raw_uid)
                            if uid_int not in user_label_map:
                                m = guild.get_member(uid_int)
                                if m is not None:
                                    user_label_map[uid_int] = m.display_name or m.name
                        except (TypeError, ValueError):
                            pass

            reflection_targets: list[dict[str, Any]] = [{
                "subject_kind": "agent",
                "subject_key": "core",
                "scope_label": "このチャンネル全体",
                "guidance_lines": [
                    "AI自身の会話方針や相手への接し方に加えて、このチャンネルで定着した呼び方・ノリ・内輪ネタも含めてください。",
                    "記憶テキストに話者やユーザー名が明記されている場合のみ、そのニックネームを主語（『〇〇さんは〜』）として用いてください。",
                    "記憶テキストに話者やユーザー名が明記されていない話題や客観的事実について、記憶に登場する単語から勝手にサーバーメンバーや第三者の名前を推測・捏造して主語に当てはめることは厳禁です。",
                    "一回きりの偶発ネタや、再利用しづらい言い回しは採用しないでください。",
                ],
                "memories": memories,
            }]

            if max_user_targets > 0:
                user_memory_groups: dict[int, list[dict[str, Any]]] = {}
                for item in memories:
                    raw_user_id = item.get("user_id")
                    if raw_user_id is None:
                        continue
                    try:
                        user_id = int(raw_user_id)
                    except (TypeError, ValueError):
                        continue
                    user_memory_groups.setdefault(user_id, []).append(item)

                ranked_user_groups = sorted(
                    user_memory_groups.items(),
                    key=lambda entry: (
                        len(entry[1]),
                        max(str(v.get("updated_at") or v.get("created_at") or "") for v in entry[1]),
                    ),
                    reverse=True,
                )
                bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
                now_ts = time.time()
                active_window_sec = float(cfg("REFLECTION_USER_ACTIVE_DAYS", 7.0) or 7.0) * 86400.0
                rel_map = getattr(self, "user_relationships", {}) or {}

                for user_id, user_memories in ranked_user_groups:
                    if len(reflection_targets) >= 1 + max_user_targets:
                        break
                    if len(user_memories) < user_min_memory_count:
                        continue
                    if bot_user_id is not None and user_id == bot_user_id:
                        continue

                    # アクティブ判定: 直近に対話実績があるか確認（休眠ユーザー・未対話メンバーの完全除外）
                    rel = rel_map.get(user_id)
                    last_interaction = float(getattr(rel, "last_interaction_ts", 0.0) or 0.0) if rel else 0.0
                    if last_interaction <= 0.0:
                        log.info(
                            "reflection target skipped (no interaction history): user_id=%s memories_count=%d",
                            user_id,
                            len(user_memories),
                        )
                        continue
                    if (now_ts - last_interaction) > active_window_sec:
                        log.info(
                            "reflection target skipped (inactive user, last active %.1f days ago): user_id=%s memories_count=%d",
                            (now_ts - last_interaction) / 86400.0,
                            user_id,
                            len(user_memories),
                        )
                        continue

                    target_user_name: str | None = None
                    if guild is not None:
                        member = guild.get_member(user_id)
                        if member is not None:
                            if getattr(member, "bot", False):
                                log.info("reflection target skipped (bot account): user_id=%s", user_id)
                                continue
                            target_user_name = member.display_name or member.name
                    if not target_user_name:
                        user = self.bot.get_user(user_id)
                        if user is not None:
                            if getattr(user, "bot", False):
                                log.info("reflection target skipped (bot user): user_id=%s", user_id)
                                continue
                            target_user_name = getattr(user, "display_name", None) or getattr(user, "name", None)

                    scope_label = f"ユーザー「{target_user_name}」さんとの関係" if target_user_name else f"ユーザー（ID: {user_id}）との関係"
                    user_guidance = [
                        "この相手への接し方、この相手が好む呼び方、その人との間で定着した言い回しや内輪ネタを優先してください。",
                        "他の相手には転用しづらい内容でも、この相手との関係で自然に再利用できるなら残してください。",
                        "単発の思いつきや、本人が嫌がりそうな呼び方は採用しないでください。",
                    ]
                    if target_user_name:
                        user_guidance.append(
                            f"対象ユーザーは『{target_user_name}』さん（ユーザーID: {user_id}）です。記憶の中に出てくる他の第三者（他のメンバーやキャラクター等）と対象ユーザーを絶対に混同しないでください。記憶に登場する別人の名前をこの相手の呼び名として採用することは厳禁です。"
                        )
                        user_guidance.append(
                            f"抽出する信念・文化の主語は、曖昧な「ユーザーは」とせず、必ず『{target_user_name}』さんは〜としてください（例: 『{target_user_name}』さんは〜を好む相手であり、〜の姿勢で接する）。"
                        )
                    elif user_id:
                        user_guidance.append(
                            f"対象ユーザーのIDは {user_id} です。信念の主語は「ユーザー（ID: {user_id}）は〜」のように特定できる形で記述してください。"
                        )
                    user_guidance.append(
                        "相手の呼び名（〜と呼ぶ等）を記録する場合は、相手自身が定着して求めている呼び方であり、かつ複数回確認できる場合（evidence_count >= 2）に限ってください。"
                    )

                    reflection_targets.append({
                        "subject_kind": "user",
                        "subject_key": str(user_id),
                        "scope_label": scope_label,
                        "target_user_name": target_user_name,
                        "target_user_id": user_id,
                        "guidance_lines": user_guidance,
                        "memories": user_memories,
                    })

            for target in reflection_targets:
                memory_lines = build_reflection_memory_lines(
                    list(target.get("memories") or []),
                    source_limit=source_limit,
                    user_label_map=user_label_map if target.get("subject_kind") == "agent" else None,
                )
                if not memory_lines:
                    continue

                target_user_name = target.get("target_user_name")
                target_user_id = target.get("target_user_id")
                prompt = build_reflection_prompt(
                    memory_lines=memory_lines,
                    max_beliefs=max_beliefs,
                    scope_label=str(target.get("scope_label") or "会話"),
                    guidance_lines=[str(line) for line in (target.get("guidance_lines") or []) if str(line).strip()],
                    target_user_name=target_user_name,
                    target_user_id=target_user_id,
                )

                try:
                    result = await call_ollama_json(
                        prompt,
                        system_prompt=self._get_reflection_system_prompt(),
                        schema=schema,
                        think=False,
                        model=reflection_model,
                        temperature=0.2,
                        timeout_sec=reflection_timeout_sec,
                        retries=reflection_retries,
                        num_predict=reflection_num_predict,
                    )
                except Exception as e:
                    log.warning(
                        "reflection target skipped: model=%s channel_id=%s subject_kind=%s subject_key=%s err=%r",
                        reflection_model,
                        reflection_channel_id,
                        target.get("subject_kind"),
                        target.get("subject_key"),
                        e,
                    )
                    continue

                beliefs = result.get("beliefs") or []
                for item in beliefs[:max_beliefs]:
                    concept = str(item.get("concept") or "").strip()
                    belief = str(item.get("belief") or "").strip()
                    if not concept or not belief:
                        continue
                    evidence_count = int(item.get("evidence_count") or 0)

                    # ユーザー紐付け正規化（多重防御）: 「ユーザーは」等の抽象主語を対象ユーザー名/IDに補正し旧エイリアスを最新名に置換
                    subject_key_str = str(target.get("subject_key") or "")
                    fallback_uid = int(subject_key_str) if (target.get("subject_kind") == "user" and subject_key_str.isdigit()) else None
                    target_uid = target_user_id or fallback_uid
                    aliases: list[str] = []
                    if target_uid is not None:
                        try:
                            from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
                            aliases = await get_user_aliases_from_store(
                                target_uid,
                                guild=guild,
                                memory_store=self.memory_store,
                            )
                        except Exception:
                            pass
                    belief = normalize_reflection_belief(
                        belief,
                        target_user_name=target_user_name,
                        target_user_id=target_uid,
                        aliases=aliases,
                    )

                    if not is_reflection_belief_valid(
                        belief,
                        evidence_count=evidence_count,
                        target_user_name=target_user_name,
                    ):
                        log.info(
                            "reflection belief rejected by validation guard: user=%s count=%d belief=%s",
                            target_user_name or target.get("subject_key"),
                            evidence_count,
                            belief,
                        )
                        continue

                    await self.memory_store.add_reflection(
                        persona=persona,
                        guild_id=guild_id,
                        channel_id=reflection_channel_id,
                        subject_kind=str(target.get("subject_kind") or "agent"),
                        subject_key=str(target.get("subject_key") or "core"),
                        concept=concept[:80],
                        belief=belief[:220],
                        importance_score=float(item.get("importance_score") or 0.0),
                        evidence_count=evidence_count,
                        source_window_start=window_start.isoformat(),
                        source_window_end=window_end.isoformat(),
                    )
