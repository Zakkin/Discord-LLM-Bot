"""OllamaChatReplyCoreMixinのエピソード記憶・長期記憶・Prospective Memory・要約更新を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import inspect
import json
import re
import time
from typing import Any, Sequence, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from front.discord.common.discord_helpers import format_message_reactions
from ollama_bot.common.emotion_helpers import (
    build_memory_emotion_tags,
    merge_emotion_scores,
)
from ollama_bot.common.ollama_helpers import (
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    looks_like_reasoning_leak,
    truncate_lines,
    truncate_text,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    author_id,
    content,
)
from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime
from front.discord.reply.reply_core_common import (
    _OllamaChatReplyCoreBase,
    _build_summary_context_lines,
    _public_attr,
    add_prospective,
    call_ollama_json,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    extract_and_store_memory,
    list_pending_prospective_memories,
    log,
    maybe_store_training_candidate,
    maybe_update_channel_summary,
    report_error,
    update_user_habit_profile,
    update_user_interests,
)


class ReplyEpisodeMixin(_OllamaChatReplyCoreBase):
    """エピソード記憶、長期記憶の抽出・保存、Prospective Memory、要約更新を担当するMixin。"""

    def _episode_importance_score(
        self,
        *,
        user_text: str,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
    ) -> float:
        peak = max((float(v or 0.0) for v in (incoming_emotion_scores or {}).values()), default=0.0)
        text = str(user_text or "").strip()
        reply = str(assistant_text or "").strip()
        importance = peak * 0.7
        if len(text) >= 30:
            importance += 0.08
        if any(marker in text for marker in ("助けて", "つらい", "しんどい", "怖い", "やばい", "ごめん", "ありがとう", "嬉しい", "かなしい", "悲しい")):
            importance += 0.12
        if any(marker in reply for marker in ("大丈夫", "落ち着", "まず", "なら", "してみ", "確認")):
            importance += 0.08
        return max(0.0, min(1.0, importance))

    def _episode_source_label(self, source: str, research_result: dict[str, Any] | None = None) -> str:
        mode = str((research_result or {}).get("mode") or source or "").strip().lower()
        if mode == "x_timeline":
            return "Xタイムライン"
        if mode == "img_top5":
            return "IMG勢い上位"
        if mode == "img_thread":
            return "IMGスレッド"
        if mode in {"browser_read_url", "browser_search", "simple_search"}:
            return "Web調査"
        if mode == "x":
            return "Xタイムライン"
        if mode == "img":
            return "IMG勢い上位"
        return "Discord"

    def _summarize_bot_action_for_episode(self, runtime: MessageRuntime, assistant_text: str) -> str:
        action_style = str(getattr(runtime, "selected_action_style", "") or "").strip()
        style_map = {
            "tease_then_answer": "軽くツッコミを入れてから答えた",
            "give_steps_first": "結論や手順を先に示した",
            "short_reaction": "短く反応した",
            "direct_reply": "率直に答えた",
            "validate_first": "まず受け止めてから答えた",
            "clarify_gently": "やわらかく確認しながら返した",
        }
        if action_style in style_map:
            return style_map[action_style]
        cleaned = truncate_text(str(assistant_text or "").strip(), 80)
        return cleaned or "返答した"

    def _build_episode_what_text(
        self,
        *,
        runtime: MessageRuntime,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> str:
        parts: list[str] = []
        user_text = truncate_text(str(runtime.original_user_text or "").strip(), 220)
        if user_text:
            parts.append(f"Discord発言: {user_text}")

        result = dict(research_result or {})
        context = dict(episode_context or {})
        query = str(result.get("query") or context.get("source_query") or "").strip()
        if query:
            parts.append(f"参照先: {truncate_text(query, 180)}")

        source_summary = str(result.get("summary") or context.get("source_summary") or "").strip()
        if source_summary:
            parts.append(f"参照内容: {truncate_text(source_summary, 420)}")

        if not parts and source:
            parts.append(f"source={source}")
        return "\n".join(parts)[:600]

    async def _maybe_store_episode_memory(
        self,
        *,
        message: discord.Message,
        runtime: MessageRuntime,
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None,
        source: str = "",
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
    ) -> None:
        if not cfg_bool("MEMORY_EPISODE_ENABLED", True):
            return
        add_episode = getattr(self.memory_store, "add_episode", None)
        if not callable(add_episode):
            return

        importance = self._episode_importance_score(
            user_text=runtime.original_user_text,
            assistant_text=assistant_text,
            incoming_emotion_scores=incoming_emotion_scores,
        )
        source_label = self._episode_source_label(source, research_result)
        if cfg_bool("MEMORY_EPISODE_STORE_ALL_TURNS", True):
            importance = max(importance, float(cfg("MEMORY_EPISODE_BASE_IMPORTANCE", 0.42) or 0.42))
        elif importance < float(cfg("MEMORY_EPISODE_STORE_THRESHOLD", 0.62) or 0.62):
            return

        emotion_tags = build_memory_emotion_tags(incoming_emotion_scores or {}, max_tags=3, min_score=0.30)
        user_text = truncate_text(runtime.original_user_text, 120)
        action_summary = self._summarize_bot_action_for_episode(runtime, assistant_text)
        _, reactions_list = format_message_reactions(message, bot_user_id=runtime.bot_user_id)
        bot_reacts = [r for r in reactions_list if r.get("is_bot")]
        if bot_reacts:
            react_names = "・".join(r["emoji"] for r in bot_reacts)
            action_summary = f"{react_names}でリアクションし、{action_summary}"
        if source_label == "Discord":
            summary = f"{user_text} に対して、{action_summary}。".strip()
        else:
            summary = f"{source_label}を見て、{user_text or 'Discord上の流れ'} に対して、{action_summary}。".strip()
        tags = list(emotion_tags)
        source_tag = str((research_result or {}).get("mode") or source or "discord").strip().lower() or "discord"
        for tag in ("episode", source_tag):
            if tag not in tags:
                tags.append(tag)

        bot_action_text = truncate_text(assistant_text or action_summary, 600)
        if bot_reacts and not any(r["emoji"] in bot_action_text for r in bot_reacts):
            bot_action_text = f"[{' '.join(r['emoji'] for r in bot_reacts)}] {bot_action_text}"

        await add_episode(
            persona=cfg_persona_namespace(custom_cfg=cfg),
            guild_id=getattr(message.guild, "id", None),
            channel_id=runtime.cid,
            user_id=author_id(message),
            summary=summary,
            what=self._build_episode_what_text(
                runtime=runtime,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            ),
            bot_action=bot_action_text,
            user_reaction=None,
            emotion_before=incoming_emotion_scores or None,
            emotion_after=None,
            importance=importance,
            tags=tags,
        )

    def _sanitize_frustrated_apology(self, reply_text: str, *, is_correction: bool, anger_count: int) -> str:
        """苛立ち・訂正時の泥沼化を防ぐため、的外れな長文謝罪を簡潔な短文謝罪・確認に整形する"""
        if not (is_correction or anger_count > 0):
            return reply_text
        apology_kws = ("すみません", "ごめんなさい", "申し訳", "お詫び")
        has_apology = any(kw in reply_text for kw in apology_kws)
        if has_apology and len(reply_text) > 80:
            sentences = re.split(r"(?<=[。！？!\?\n])", reply_text)
            for s in sentences:
                s_clean = s.strip()
                if any(kw in s_clean for kw in apology_kws) and len(s_clean) <= 60:
                    return s_clean
            return "ごめん、勘違いしてた！教えてくれてありがとう。"
        return reply_text

    async def _record_correction_feedback_if_needed(
        self,
        *,
        message: discord.Message,
        runtime: MessageRuntime,
        assistant_text: str,
        intent_info: dict[str, Any] | None,
    ) -> None:
        """ユーザー訂正時の学習フィードバック記録・1時間抑止・苛立ちシグナル記録"""
        user_text = getattr(runtime, "effective_user_text", None) or getattr(runtime, "original_user_text", None) or content(message)
        # 1. 苛立ちシグナル記録
        try:
            from front.discord.common.frustration import count_anger_markers, record_user_signal
            anger_count = count_anger_markers(user_text)
            is_breakdown = bool(intent_info and intent_info.get("breakdown"))
            is_correction = bool(intent_info and intent_info.get("user_correction"))
            g_id = getattr(message.guild, "id", 0) or 0
            record_user_signal(
                guild_id=g_id,
                channel_id=runtime.cid or 0,
                user_id=author_id(message) or 0,
                is_correction=is_correction,
                is_breakdown=is_breakdown,
                anger_markers=anger_count,
            )
        except Exception as e:
            log.debug("record_user_signal failed: %r", e)

        # 2. 訂正フィードバック記録
        if not (intent_info and intent_info.get("user_correction")):
            return
        if not hasattr(self, "memory_store") or self.memory_store is None:
            return

        corr_data = intent_info.get("user_correction")
        claim_text = ""
        corrected_fact = ""
        if isinstance(corr_data, dict):
            claim_text = str(corr_data.get("claim", "")).strip()
            corrected_fact = str(corr_data.get("corrected_fact", "")).strip()
        if not claim_text:
            claim_text = user_text

        claim_key = re.sub(r"[\s、。！？!?,.]+", "", str(claim_text))[:40].lower()
        now_ts = time.time()

        # 否定済みレジストリへ登録
        try:
            from harness.agent.prompt_parser.prompt_manager.denied_claims import register_denied_claim
            p_name = cfg_persona_namespace(custom_cfg=cfg)
            c_key = str(runtime.cid or "default")
            register_denied_claim(persona=p_name, channel_key=c_key, claim=claim_text, corrected_fact=corrected_fact)
        except Exception as e:
            log.debug("register_denied_claim skipped: %r", e)

        # 1時間以内の同一訂正抑止チェック
        recent_corrections: list[dict[str, Any]] = []
        try:
            get_state = getattr(self.memory_store, "get_conversation_state", None)
            if callable(get_state):
                state = await get_state(channel_id=runtime.cid, key="user_corrections")
                if isinstance(state, list):
                    recent_corrections = list(state)
                elif isinstance(state, dict):
                    recent_corrections = list(state.get("items", []))
        except Exception as e:
            log.debug("get_conversation_state failed: %r", e)

        is_suppressed = False
        one_hour_ago = now_ts - 3600.0
        same_claim_count = 0
        for item in recent_corrections:
            if item.get("claim_key") == claim_key:
                same_claim_count += 1
                if float(item.get("ts", 0.0)) > one_hour_ago:
                    is_suppressed = True

        if not is_suppressed:
            try:
                add_fb = getattr(self.memory_store, "add_feedback_item", None)
                if callable(add_fb):
                    await add_fb(
                        persona=cfg_persona_namespace(custom_cfg=cfg),
                        guild_id=getattr(message.guild, "id", None),
                        channel_id=runtime.cid,
                        user_id=author_id(message),
                        original_user_text=runtime.original_user_text,
                        assistant_text=assistant_text,
                        feedback_text=user_text,
                        corrected_assistant_text=corrected_fact or None,
                        feedback_type="user_correction",
                        accepted=True,
                    )
            except Exception as e:
                log.warning("add_feedback_item user_correction failed: %r", e)

        recent_corrections.append({"claim_key": claim_key, "ts": now_ts, "claim": claim_text})
        same_claim_count += 1
        if same_claim_count >= 3:
            log.warning("[CORRECTION_REPEAT] Repeated correction detected (%d times) for claim_key=%s", same_claim_count, claim_key)

        try:
            set_state = getattr(self.memory_store, "set_conversation_state", None)
            if callable(set_state):
                await set_state(
                    channel_id=runtime.cid,
                    key="user_corrections",
                    value=recent_corrections[-50:],
                )
        except Exception as e:
            log.debug("set_conversation_state failed: %r", e)

    def _get_thread_context_for_working_memory(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        bot_identity: Any,
    ) -> tuple[list[dict[str, Any]], str | None]:
        tracker = getattr(self, "thread_tracker", None)
        active_threads_data: list[dict[str, Any]] = []
        current_thread_topic: str | None = None
        if tracker and runtime.cid is not None:
            get_active_threads_fn = getattr(tracker, "get_active_threads", None)
            if callable(get_active_threads_fn):
                raw_active = get_active_threads_fn(runtime.cid, before_message=message)
                active_threads_data = [
                    {
                        "topic": getattr(t, "topic", "雑談"),
                        "participants": list(getattr(t, "participant_names", [])),
                    }
                    for t in (raw_active or [])
                ]
            get_origin_ctx_fn = getattr(tracker, "get_message_origin_context", None)
            if callable(get_origin_ctx_fn):
                origin_ctx = get_origin_ctx_fn(message, bot_user_id=runtime.bot_user_id, bot_identity=bot_identity)
                current_thread_topic = origin_ctx.get("current_topic")
        return active_threads_data, current_thread_topic

    async def _maybe_extract_and_store_prospective_memory(
        self,
        message: discord.Message,
        *,
        runtime: MessageRuntime,
        context_lines: list[str],
        user_display_name: str | None = None,
    ) -> None:
        if not cfg_bool("PROSPECTIVE_FOLLOWUP_ENABLED", True):
            return

        store = getattr(self, "memory_store", None)
        if store is None:
            return

        u_id = author_id(message)
        if u_id is None:
            return

        user_text = str(getattr(runtime, "original_user_text", "") or getattr(message, "content", "") or "").strip()
        if not user_text:
            return

        from datetime import datetime, timezone, timedelta
        jst = timezone(timedelta(hours=9))
        now_jst = datetime.now(jst)
        today_str = now_jst.strftime("%Y-%m-%d")

        schema = {
            "type": "object",
            "properties": {
                "has_future_event": {"type": "boolean"},
                "event_text": {"type": "string"},
                "when_text": {"type": "string"},
                "due_date": {"type": "string"},
                "cue_keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "confidence": {"type": "number"},
                "action_type": {
                    "type": "string",
                    "description": "自発行動の種別: post_followup (事後の結果尋ね・感想確認), same_day_morning (当日朝の応援・気遣い), same_day_evening (当日夜の労い・お疲れ様), reminder (特定日時のリマインド・通知), general_inquiry (一般的な話題再開)",
                },
                "intent": {
                    "type": "string",
                    "description": "Botがその時どう話しかけるべきかの具体的な意図・目標",
                },
                "trigger_timing": {
                    "type": "string",
                    "description": "発火させる最適なタイミング: next_day_morning (翌朝09:00), same_day_morning (当日朝08:30), same_day_evening (当日夜20:00), exact_date (当日)",
                },
                "kind": {
                    "type": "string",
                    "description": "記憶の種別: time_based または event_based",
                },
            },
            "required": [
                "has_future_event",
                "event_text",
                "when_text",
                "due_date",
                "cue_keywords",
                "confidence",
            ],
        }

        system_prompt = (
            "あなたはユーザーとの会話から、ユーザーの予定、未来のイベント、約束、リマインド、関心事を正確に抽出し、"
            "適切なタイミングでBotが自発的に声掛け・フォローアップ・応援・リマインドするための情報を構造化するアシスタントです。"
            "指定されたJSON形式でのみ回答してください。"
        )

        prompt_lines = [
            f"【基準日(本日)】: {today_str} (JST)",
            f"【ユーザー発言】: {user_text}",
        ]
        if context_lines:
            ctx_snip = "\n".join(context_lines[-3:])
            prompt_lines.append(f"【直前の会話文脈】:\n{ctx_snip}")
        prompt_lines.append(
            "\n【指示】\n"
            "- ユーザー自身が近未来に行う予定・イベント・約束・リマインド希望がある場合は has_future_event: true とし、詳細を抽出してください。\n"
            "- due_date は基準日(本日)を元に、そのイベントの実施日（または約束・リマインドの日）を YYYY-MM-DD 形式で計算してください。\n"
            "- action_type と trigger_timing に最も適切な行動種別と発火タイミングを選定してください:\n"
            "  * 事後の結果確認・感想尋ね: action_type='post_followup', trigger_timing='next_day_morning'（翌朝9時）\n"
            "  * 当日直前の応援・声掛け: action_type='same_day_morning', trigger_timing='same_day_morning'（当日朝8時30分）\n"
            "  * 当日終了後の労い: action_type='same_day_evening', trigger_timing='same_day_evening'（当日夜20時）\n"
            "  * リマインド・通知: action_type='reminder', trigger_timing='same_day_morning'（または exact_date）\n"
            "- intent には「誰の何について、いつ、どのように自発的に話しかけるか」の具体的な意図を記述してください。\n"
            "- 該当する予定がない場合は has_future_event: false, event_text: '', when_text: '', due_date: '', cue_keywords: [], confidence: 0.0 を返してください。"
        )
        prompt = "\n".join(prompt_lines)

        middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm")
        try:
            result = await call_ollama_json(
                prompt,
                system_prompt=system_prompt,
                schema=schema,
                model=middle_model,
                temperature=0.0,
                think=False,
                timeout_sec=float(cfg("PROSPECTIVE_EXTRACTION_TIMEOUT_SEC", 30.0) or 30.0),
            )
        except Exception as e:
            log.warning("prospective memory extraction failed: %r", e)
            return

        if not isinstance(result, dict):
            log.info("prospective memory skipped: result is not a dict (%r)", result)
            return

        has_future_event = bool(result.get("has_future_event", False))
        confidence = float(result.get("confidence") or 0.0)
        due_date_str = str(result.get("due_date") or "").strip()
        event_text = str(result.get("event_text") or "").strip()
        when_text = str(result.get("when_text") or "").strip()
        raw_keywords = result.get("cue_keywords") or []
        if isinstance(raw_keywords, list):
            cue_keywords = [str(k).strip() for k in raw_keywords if str(k).strip()]
        else:
            cue_keywords = [str(raw_keywords).strip()] if str(raw_keywords).strip() else []

        if not has_future_event:
            log.info("prospective memory skipped: has_future_event is False (user_id=%s)", u_id)
            return

        if confidence < 0.7:
            log.info("prospective memory skipped: low confidence %.2f < 0.7 (user_id=%s)", confidence, u_id)
            return

        if not due_date_str:
            log.info("prospective memory skipped: due_date is empty (user_id=%s)", u_id)
            return

        try:
            due_date_obj = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        except ValueError:
            log.info("prospective memory skipped: invalid due_date format '%s' (user_id=%s)", due_date_str, u_id)
            return

        if due_date_obj <= now_jst.date():
            log.info(
                "prospective memory skipped: due_date '%s' is not in the future (today is %s, user_id=%s)",
                due_date_str,
                today_str,
                u_id,
            )
            return

        action_type = str(result.get("action_type") or "").strip().lower()
        trigger_timing = str(result.get("trigger_timing") or "").strip().lower()

        # 発火タイミングに応じた due_at と expires_at の動的計算
        if trigger_timing in ("same_day_morning", "morning") or action_type in ("same_day_morning", "cheer", "encouragement"):
            # イベント当日の朝 08:30 JST に発火、当日夕方 18:00 に期限切れ
            due_at_dt = datetime(due_date_obj.year, due_date_obj.month, due_date_obj.day, 8, 30, 0, tzinfo=jst)
            expires_at_dt = datetime(due_date_obj.year, due_date_obj.month, due_date_obj.day, 18, 0, 0, tzinfo=jst)
        elif trigger_timing in ("same_day_evening", "evening", "night") or action_type in ("same_day_evening", "comfort", "congratulation"):
            # イベント当日の夜 20:00 JST に発火、翌日正午に期限切れ
            due_at_dt = datetime(due_date_obj.year, due_date_obj.month, due_date_obj.day, 20, 0, 0, tzinfo=jst)
            expires_at_dt = due_at_dt + timedelta(hours=16)
        else:
            # デフォルト: イベント翌日の朝 09:00 JST に発火（事後フォローアップ・結果確認）、3日後に期限切れ
            next_day = due_date_obj + timedelta(days=1)
            due_at_dt = datetime(next_day.year, next_day.month, next_day.day, 9, 0, 0, tzinfo=jst)
            expires_at_dt = due_at_dt + timedelta(days=3)

        due_at_iso = due_at_dt.isoformat()
        expires_at_iso = expires_at_dt.isoformat()

        persona = cfg_persona_namespace(custom_cfg=cfg) if callable(cfg_persona_namespace) else str(cfg("OLLAMA_BOT_PERSONA", "default") or "default")
        pending_records: list[dict[str, Any]] = []
        try:
            pending_records = list_pending_prospective_memories(
                store,
                persona=persona,
                target_user_id=u_id,
            ) or []
        except TypeError:
            pending_records = list_pending_prospective_memories(
                persona=persona,
                target_user_id=u_id,
            ) or []
        except Exception as e:
            log.warning("list_pending_prospective_memories failed: %r", e)

        max_per_user = cfg_int("FOLLOWUP_MAX_PER_USER", 5)
        if len(pending_records) >= max_per_user:
            log.info("prospective memory skipped: user %s reached max pending limit (%d >= %d)", u_id, len(pending_records), max_per_user)
            return

        extracted_kws_set = {k.lower() for k in cue_keywords}
        for rec in pending_records:
            rec_kws = rec.get("cue_keywords") or []
            if isinstance(rec_kws, str):
                import json
                try:
                    rec_kws = json.loads(rec_kws)
                except Exception:
                    rec_kws = [k.strip() for k in rec_kws.split(",") if k.strip()]
            rec_kws_set = {str(k).lower() for k in rec_kws if str(k).strip()}
            overlap = extracted_kws_set & rec_kws_set
            if overlap:
                log.info("prospective memory skipped: duplicate cue_keywords %s (overlap=%s) for user %s", cue_keywords, list(overlap), u_id)
                return

        author_name = user_display_name or self._resolve_author_display_name(message) or getattr(message.author, "name", "ユーザー")
        extracted_intent = str(result.get("intent") or "").strip()
        if extracted_intent:
            intent = extracted_intent
        elif action_type in ("same_day_morning", "cheer"):
            intent = f"{author_name}の「{event_text}」（{when_text}）の直前に、応援や気遣いの言葉をかける"
        elif action_type in ("same_day_evening", "comfort"):
            intent = f"{author_name}の「{event_text}」（{when_text}）の終了後に、労いやお疲れ様の言葉をかける"
        elif action_type == "reminder":
            intent = f"{author_name}に「{event_text}」（{when_text}）の予定・リマインドを通知する"
        else:
            intent = f"{author_name}の「{event_text}」（{when_text}）について、その後の様子や結果を尋ねてフォローアップする"

        raw_kind = str(result.get("kind") or "").strip().lower()
        kind = raw_kind if raw_kind in ("time_based", "event_based") else "time_based"
        priority = max(1, min(10, int(confidence * 10)))

        record_id = None
        try:
            record_id = add_prospective(
                store,
                persona=persona,
                guild_id=getattr(message.guild, "id", None),
                channel_id=runtime.cid,
                target_user_id=u_id,
                kind=kind,
                cue_keywords=cue_keywords,
                cue_topic_tag=event_text,
                intent=intent,
                priority=priority,
                due_at=due_at_iso,
                expires_at=expires_at_iso,
            )
        except TypeError:
            record_id = add_prospective(
                persona=persona,
                guild_id=getattr(message.guild, "id", None),
                channel_id=runtime.cid,
                target_user_id=u_id,
                kind=kind,
                cue_keywords=cue_keywords,
                cue_topic_tag=event_text,
                intent=intent,
                priority=priority,
                due_at=due_at_iso,
                expires_at=expires_at_iso,
            )

        log.info(
            "prospective memory registered: record_id=%s, user_id=%s, event='%s', when='%s', due_at=%s, expires_at=%s",
            record_id,
            u_id,
            event_text,
            when_text,
            due_at_iso,
            expires_at_iso,
        )

    async def _store_and_update_summary(
        self,
        message: discord.Message,
        sent: discord.Message,
        *,
        runtime: MessageRuntime,
        context_lines: list[str],
        assistant_text: str,
        incoming_emotion_scores: dict[str, float] | None = None,
        spontaneous_source: str | None = None,
        research_result: dict[str, Any] | None = None,
        episode_context: dict[str, Any] | None = None,
        intent_info: dict[str, Any] | None = None,
    ) -> None:
        if looks_like_abnormal_assistant_reply(assistant_text):
            return
        skip_long_term_memory = str(spontaneous_source or "").strip().lower() == "x"
        source = (
            str((research_result or {}).get("mode") or "").strip()
            or str(spontaneous_source or "").strip()
            or "discord"
        )

        cfg_fn = _public_attr("cfg", cfg)
        trimmed_context_lines = truncate_lines(
            context_lines,
            max_lines=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
            max_chars_per_line=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
            max_total_chars=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
        )
        memory_context_lines = self._trim_memory_context_lines(
            runtime.memory_context_lines or context_lines
        ) or trimmed_context_lines

        get_snapshot = getattr(self, "_get_emotion_snapshot", None)
        if callable(get_snapshot):
            snap = get_snapshot()
            if inspect.isawaitable(snap):
                snap = await snap
            emotion_scores, _, emotion_reason = snap if isinstance(snap, tuple) and len(snap) >= 3 else ({}, None, "")
        else:
            emotion_scores, emotion_reason = {}, ""
        emotion_scores = merge_emotion_scores(emotion_scores, incoming_emotion_scores)

        user_name = getattr(getattr(message, "author", None), "name", None)
        user_display_name = self._resolve_author_display_name(message) or user_name

        if not skip_long_term_memory:
            self._ensure_background_llm_serialization()
            yield_fn = getattr(self, "_yield_to_user_replies", None)
            if callable(yield_fn):
                await yield_fn()

            thought_wait_start = time.time()
            thought_last_logged = thought_wait_start
            thought_max_wait = max(30.0, cfg_float("LLM_YIELD_MAX_WAIT_SEC", 300.0))
            while getattr(self, "_is_thought_generation_active", False):
                elapsed = time.time() - thought_wait_start
                if elapsed >= thought_max_wait:
                    log.warning("_store_and_update_summary: waiting for background thought generation reached maximum limit (%.1fs); proceeding", thought_max_wait)
                    break
                if elapsed >= 30.0 and time.time() - thought_last_logged >= 15.0:
                    thought_last_logged = time.time()
                    log.info("_store_and_update_summary: background thought generation still active (elapsed=%.1fs); safely extending wait...", elapsed)
                await asyncio.sleep(0.2)

            if callable(yield_fn):
                await yield_fn()
            setattr(self, "_is_memory_extraction_active", True)
            try:
                extract_fn = _public_attr("extract_and_store_memory", extract_and_store_memory)
                await extract_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    emotion_scores=emotion_scores,
                    emotion_reason=emotion_reason,
                    user_name=user_name,
                    user_display_name=user_display_name,
                )
            except Exception as e:
                log.exception("memory extract failed: %s", e)
                report_error("reply_core.extract_and_store_memory", e)
            finally:
                setattr(self, "_is_memory_extraction_active", False)

            if callable(yield_fn):
                await yield_fn()
            setattr(self, "_is_memory_extraction_active", True)
            try:
                await self._maybe_extract_and_store_prospective_memory(
                    message=message,
                    runtime=runtime,
                    context_lines=memory_context_lines,
                    user_display_name=user_display_name,
                )
            except Exception as e:
                log.exception("prospective memory extract failed: %s", e)
                report_error("reply_core.prospective_memory_extract", e)
            finally:
                setattr(self, "_is_memory_extraction_active", False)

        try:
            await self._maybe_store_episode_memory(
                message=message,
                runtime=runtime,
                assistant_text=assistant_text,
                incoming_emotion_scores=incoming_emotion_scores,
                source=source,
                research_result=research_result,
                episode_context=episode_context,
            )
        except Exception as e:
            log.exception("episode memory store failed: %s", e)
            report_error("reply_core._maybe_store_episode_memory", e)

        try:
            effective_intent_info = (
                intent_info
                if intent_info is not None
                else (episode_context if isinstance(episode_context, dict) else (getattr(runtime, "intent_info", None) or {}))
            )
            await self._record_correction_feedback_if_needed(
                message=message,
                runtime=runtime,
                assistant_text=assistant_text,
                intent_info=effective_intent_info,
            )
        except Exception as e:
            log.exception("correction feedback record failed: %s", e)
            report_error("reply_core._record_correction_feedback_if_needed", e)

        highest_emotion_score = max(emotion_scores.values()) if emotion_scores else 0.0
        if highest_emotion_score >= 0.8:
            deep_reflection = getattr(self, "_run_deep_reflection_once", None)
            if callable(deep_reflection):
                _reflection_task = asyncio.create_task(deep_reflection(
                    channel_id=runtime.cid,
                    emotion_scores=emotion_scores,
                    emotion_reason=emotion_reason or "不明",
                ))
                def _on_reflection_done(t: asyncio.Task[Any]) -> None:
                    with contextlib.suppress(asyncio.CancelledError):
                        exc = t.exception()
                        if exc:
                            log.warning("deep reflection task failed: %r", exc)
                            report_error("reply_core.deep_reflection", exc)

                _reflection_task.add_done_callback(_on_reflection_done)

        candidate_saved = False
        if not skip_long_term_memory:
            try:
                store_candidate_fn = _public_attr("maybe_store_training_candidate", maybe_store_training_candidate)
                candidate_saved = await store_candidate_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=trimmed_context_lines,
                    looks_like_reasoning_leak=looks_like_reasoning_leak,
                    looks_like_parrot_reply=looks_like_parrot_reply,
                    is_weather_reply=runtime.is_weather_reply,
                )
            except Exception as e:
                log.exception("training candidate save failed: %s", e)
                report_error("reply_core.training_candidate_save", e)
                candidate_saved = False

        if candidate_saved:
            try:
                update_habit_fn = _public_attr("update_user_habit_profile", update_user_habit_profile)
                await update_habit_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    assistant_text=assistant_text,
                    context_lines=memory_context_lines,
                    candidate_reason="auto-saved training candidate",
                    user_name=user_name,
                    user_display_name=user_display_name,
                )
            except Exception as e:
                log.exception("habit profile update failed: %s", e)
                report_error("reply_core.habit_profile_update", e)

        try:
            build_summary_fn = _public_attr("_build_summary_context_lines", _build_summary_context_lines)
            summary_context_lines = await build_summary_fn(
                message,
                sent,
                base_context_lines=memory_context_lines,
                bot_user_id=runtime.bot_user_id,
                fetch_limit=max(
                    int(cfg_fn("MEMORY_SUMMARY_CONTEXT_LINES", cfg_fn("MEMORY_LONG_CONTEXT_LINES", 30)) or 30),
                    int(cfg_fn("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20),
                ),
            )
        except Exception as e:
            log.exception("summary context build failed: %s", e)
            report_error("reply_core.summary_context_build", e)
            summary_context_lines = memory_context_lines

        try:
            update_summary_fn = _public_attr("maybe_update_channel_summary", maybe_update_channel_summary)
            await update_summary_fn(
                cfg=cfg_fn,
                store=self.memory_store,
                guild_id=getattr(message.guild, "id", None),
                channel_id=runtime.cid,
                context_lines=summary_context_lines,
            )
        except Exception as e:
            log.exception("channel summary update failed: %s", e)
            report_error("reply_core.channel_summary_update", e)

        if not skip_long_term_memory:
            try:
                update_interests_fn = _public_attr("update_user_interests", update_user_interests)
                await update_interests_fn(
                    cfg=cfg_fn,
                    store=self.memory_store,
                    guild_id=getattr(message.guild, "id", None),
                    channel_id=runtime.cid,
                    user_id=author_id(message),
                    user_text=runtime.original_user_text,
                    context_lines=memory_context_lines,
                )
            except Exception as e:
                log.exception("user interests update failed: %s", e)
                report_error("reply_core.user_interests_update", e)

        tracker = getattr(self, "thread_tracker", None)
        save_thread = getattr(self.memory_store, "save_channel_thread", None)
        if tracker and callable(save_thread) and runtime.cid is not None:
            try:
                persona_ns = cfg_persona_namespace(custom_cfg=cfg_fn)
                get_active_threads_fn = getattr(tracker, "get_active_threads", None)
                active_threads = get_active_threads_fn(runtime.cid) if callable(get_active_threads_fn) else []
                for th in active_threads:
                    await save_thread(
                        persona=persona_ns,
                        guild_id=getattr(message.guild, "id", None),
                        channel_id=runtime.cid,
                        thread_key=getattr(th, "thread_id", str(th)),
                        topic=getattr(th, "topic", ""),
                        participants=getattr(th, "participant_names", []),
                        turns=list(getattr(th, "turns", [])),
                        last_active_ts=getattr(th, "last_active_ts", None),
                    )
            except Exception as e:
                log.exception("channel threads persistence failed: %s", e)
                report_error("reply_core.channel_threads_persistence", e)

        counts = getattr(self, "_passive_channel_message_counts", None)
        if isinstance(counts, dict) and runtime.cid is not None:
            counts[runtime.cid] = 0

        try:
            mark_future = getattr(self, "_mark_future_prompts_triggered", None)
            if callable(mark_future):
                await mark_future(runtime)
        except Exception as e:
            log.exception("prospective trigger update failed: %s", e)

        if not skip_long_term_memory:
            remember_habit = getattr(self, "_remember_pending_habit_turn", None)
            if callable(remember_habit):
                remember_habit(runtime, incoming_emotion_scores=incoming_emotion_scores)

    async def _get_user_aliases_for_message(self, message: discord.Message) -> tuple[int | None, list[str]]:
        uid = author_id(message)
        if uid is None:
            return None, []
        try:
            from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
            aliases = await get_user_aliases_from_store(
                uid,
                guild=getattr(message, "guild", None),
                memory_store=getattr(self, "memory_store", None),
            )
            return uid, aliases
        except Exception:
            return uid, []

