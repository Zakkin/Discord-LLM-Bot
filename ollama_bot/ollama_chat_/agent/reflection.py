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
)
from ...common.discord_helpers import author_id, channel_id
from ...common.emotion_helpers import (
    build_memory_emotion_tags,
    format_emotion_tags_ja,
)
from ...common.ollama_helpers import call_ollama_json
from ...common.reflection_logic import (
    DEEP_REFLECTION_SYSTEM_PROMPT,
    DEFAULT_REFLECTION_SYSTEM_PROMPT,
    build_deep_reflection_prompt,
    build_reflection_memory_lines,
    build_reflection_prompt,
    merge_reflection_rows,
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
        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default")
        guild_id = getattr(message.guild, "id", None)
        cid = channel_id(message)
        max_items = int(cfg("REFLECTION_PROMPT_MAX_BELIEFS", 3) or 3)
        user_limit = max_items if max_items <= 1 else max_items - 1
        min_importance = float(cfg("REFLECTION_PROMPT_MIN_IMPORTANCE", 0.45) or 0.45)
        rows_by_scope: list[list[dict[str, Any]]] = []

        uid = author_id(message)
        if uid is not None:
            rows_by_scope.append(await self.memory_store.list_reflections(
                persona=persona,
                guild_id=guild_id,
                channel_id=cid,
                subject_kind="user",
                subject_key=str(uid),
                limit=user_limit,
                min_importance=min_importance,
            ))

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

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default")
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

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default")
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

            reflection_targets: list[dict[str, Any]] = [{
                "subject_kind": "agent",
                "subject_key": "core",
                "scope_label": "このチャンネル全体",
                "guidance_lines": [
                    "AI自身の会話方針や相手への接し方に加えて、このチャンネルで定着した呼び方・ノリ・内輪ネタも含めてください。",
                    "特定のユーザーとの間で定着したニックネームや言い回しがあるなら、誰に対するものか分かる短い形でまとめてください。",
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
                for user_id, user_memories in ranked_user_groups[:max_user_targets]:
                    if len(user_memories) < user_min_memory_count:
                        continue
                    reflection_targets.append({
                        "subject_kind": "user",
                        "subject_key": str(user_id),
                        "scope_label": "ある特定ユーザーとの関係",
                        "guidance_lines": [
                            "この相手への接し方、この相手が好む呼び方、その人との間で定着した言い回しや内輪ネタを優先してください。",
                            "他の相手には転用しづらい内容でも、この相手との関係で自然に再利用できるなら残してください。",
                            "単発の思いつきや、本人が嫌がりそうな呼び方は採用しないでください。",
                        ],
                        "memories": user_memories,
                    })

            for target in reflection_targets:
                memory_lines = build_reflection_memory_lines(
                    list(target.get("memories") or []),
                    source_limit=source_limit,
                )
                if not memory_lines:
                    continue

                prompt = build_reflection_prompt(
                    memory_lines=memory_lines,
                    max_beliefs=max_beliefs,
                    scope_label=str(target.get("scope_label") or "会話"),
                    guidance_lines=[str(line) for line in (target.get("guidance_lines") or []) if str(line).strip()],
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
                    await self.memory_store.add_reflection(
                        persona=persona,
                        guild_id=guild_id,
                        channel_id=reflection_channel_id,
                        subject_kind=str(target.get("subject_kind") or "agent"),
                        subject_key=str(target.get("subject_key") or "core"),
                        concept=concept[:80],
                        belief=belief[:220],
                        importance_score=float(item.get("importance_score") or 0.0),
                        evidence_count=int(item.get("evidence_count") or 0),
                        source_window_start=window_start.isoformat(),
                        source_window_end=window_end.isoformat(),
                    )
