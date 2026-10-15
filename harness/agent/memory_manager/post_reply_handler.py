"""AIエージェント・ハーネス 返信後メモリ・要約永続化ハンドラー (post_reply_handler.py)

フェーズ5完了後に対話ログから長期記憶抽出、エピソード記憶保存、
チャンネル要約更新、習慣プロファイル更新、スレッド永続化等を安全に実行する。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from lib.config_utils import cfg, cfg_bool, cfg_persona_namespace
from lib.dispatch_utils import get_public_attr
from ollama_bot.common.discord_helpers import author_id, display_name, format_message_reactions
from ollama_bot.common.emotion_helpers import build_memory_emotion_tags, merge_emotion_scores
from ollama_bot.common.ollama_helpers import (
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    looks_like_reasoning_leak,
    truncate_lines,
    truncate_text,
)

from .extractor import extract_and_store_memory as _default_extract_and_store_memory
from .summarizer import (
    maybe_store_training_candidate as _default_maybe_store_training_candidate,
    maybe_update_channel_summary as _default_maybe_update_channel_summary,
    update_user_habit_profile as _default_update_user_habit_profile,
)

log = logging.getLogger("harness.agent.memory_manager.post_reply_handler")

_PUBLIC_REPLY_MODULE = "ollama_bot.ollama_chat_.ollama_chat_reply"


def _public_fn(name: str, default: Any) -> Any:
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)


def episode_importance_score(
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


def episode_source_label(source: str, research_result: dict[str, Any] | None = None) -> str:
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


def summarize_bot_action_for_episode(runtime: Any, assistant_text: str) -> str:
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


def build_episode_what_text(
    *,
    runtime: Any,
    source: str = "",
    research_result: dict[str, Any] | None = None,
    episode_context: dict[str, Any] | None = None,
) -> str:
    parts: list[str] = []
    user_text = truncate_text(str(getattr(runtime, "original_user_text", "") or "").strip(), 220)
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


async def maybe_store_episode_memory(
    store: Any,
    *,
    message: Any,
    runtime: Any,
    assistant_text: str,
    incoming_emotion_scores: dict[str, float] | None = None,
    source: str = "",
    research_result: dict[str, Any] | None = None,
    episode_context: dict[str, Any] | None = None,
) -> None:
    cfg_fn = _public_fn("cfg", cfg)
    cfg_bool_fn = _public_fn("cfg_bool", cfg_bool)
    if not cfg_bool_fn("MEMORY_EPISODE_ENABLED", True):
        return
    add_episode = getattr(store, "add_episode", None)
    if not callable(add_episode):
        return

    importance = episode_importance_score(
        user_text=str(getattr(runtime, "original_user_text", "") or ""),
        assistant_text=assistant_text,
        incoming_emotion_scores=incoming_emotion_scores,
    )
    source_label = episode_source_label(source, research_result)
    if cfg_bool_fn("MEMORY_EPISODE_STORE_ALL_TURNS", True):
        importance = max(importance, float(cfg_fn("MEMORY_EPISODE_BASE_IMPORTANCE", 0.42) or 0.42))
    elif importance < float(cfg_fn("MEMORY_EPISODE_STORE_THRESHOLD", 0.62) or 0.62):
        return

    emotion_tags = build_memory_emotion_tags(incoming_emotion_scores or {}, max_tags=3, min_score=0.30)
    user_text = truncate_text(str(getattr(runtime, "original_user_text", "") or ""), 120)
    action_summary = summarize_bot_action_for_episode(runtime, assistant_text)
    _, reactions_list = format_message_reactions(message, bot_user_id=getattr(runtime, "bot_user_id", None))
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
        persona=cfg_persona_namespace(custom_cfg=cfg_fn),
        guild_id=getattr(message.guild, "id", None) if getattr(message, "guild", None) else None,
        channel_id=getattr(runtime, "cid", None),
        user_id=author_id(message),
        summary=summary,
        what=build_episode_what_text(
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


def trim_memory_context_lines(lines: list[str]) -> list[str]:
    raw_lines = [str(x) for x in (lines or []) if str(x).strip()]
    if not raw_lines:
        return []
    cfg_fn = _public_fn("cfg", cfg)
    max_lines = max(1, int(cfg_fn("MEMORY_EXTRACT_CONTEXT_MAX_LINES", 6) or 6))
    return raw_lines[-max_lines:]


async def handle_post_reply_memory_and_summary(
    bot_cog: Any = None,
    message: Any = None,
    sent: Any = None,
    *,
    runtime: Any = None,
    context_lines: list[str] | None = None,
    assistant_text: str = "",
    incoming_emotion_scores: dict[str, float] | None = None,
    spontaneous_source: str | None = None,
    research_result: dict[str, Any] | None = None,
    episode_context: dict[str, Any] | None = None,
    memory_store: Any = None,
    channel_context_cache: Any = None,
    thread_tracker: Any = None,
    get_emotion_snapshot_fn: Any = None,
    deep_reflection_fn: Any = None,
    mark_prompts_fn: Any = None,
    remember_habit_fn: Any = None,
    build_summary_lines_fn: Any = None,
    extract_memory_fn: Any = None,
    store_candidate_fn: Any = None,
    update_habit_fn: Any = None,
    update_interests_fn: Any = None,
    update_summary_fn: Any = None,
) -> None:
    """返信後の長期記憶抽出、エピソード保存、要約更新、習慣プロファイル更新を実行する統合エントリーポイント。

    bot_cog の内部状態に直接依存せず、明示的に渡されたパラメータ（memory_store, thread_tracker 等）
    を用いて自立実行可能。従来の bot_cog 経由呼び出しとの完全な後方互換性も保持する。
    """
    if looks_like_abnormal_assistant_reply(assistant_text):
        return

    cfg_fn = _public_fn("cfg", cfg)

    # 1. memory_store の解決
    store = memory_store or getattr(bot_cog, "memory_store", None)
    if store is None:
        log.warning("handle_post_reply_memory_and_summary: memory_store is not provided and not found on bot_cog")
        return

    # 2. 関数類の解決（明示指定 > Memory Manager デフォルト実装）
    actual_extract_memory_fn = extract_memory_fn or _default_extract_and_store_memory
    actual_store_candidate_fn = store_candidate_fn or _default_maybe_store_training_candidate
    actual_update_habit_fn = update_habit_fn or _default_update_user_habit_profile
    if update_interests_fn is not None:
        actual_update_interests_fn = update_interests_fn
    else:
        from harness.agent.prompt_parser.prompt_manager.user_interest_logic import (
            update_user_interests as _default_update_user_interests,
        )
        actual_update_interests_fn = _default_update_user_interests
    actual_update_summary_fn = update_summary_fn or _default_maybe_update_channel_summary

    skip_long_term_memory = str(spontaneous_source or "").strip().lower() == "x"
    source = (
        str((research_result or {}).get("mode") or "").strip()
        or str(spontaneous_source or "").strip()
        or "discord"
    )

    raw_context = list(context_lines or [])
    trimmed_context_lines = truncate_lines(
        raw_context,
        max_lines=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
        max_chars_per_line=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
        max_total_chars=int(cfg_fn("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
    )
    runtime_context = getattr(runtime, "memory_context_lines", None) if runtime is not None else None
    memory_context_lines = trim_memory_context_lines(
        runtime_context or raw_context
    ) or trimmed_context_lines

    # 3. 感情スナップショットの取得（bot_cog なしでも安全にフォールバック）
    emotion_scores: dict[str, float] = {}
    emotion_reason: str = ""
    snapshot_func = get_emotion_snapshot_fn or getattr(bot_cog, "_get_emotion_snapshot", None)
    if callable(snapshot_func):
        try:
            snapshot_res = snapshot_func()
            if inspect.isawaitable(snapshot_res):
                snapshot_res = await snapshot_res
            if isinstance(snapshot_res, tuple):
                if len(snapshot_res) >= 3:
                    emotion_scores, _, emotion_reason = snapshot_res[0], snapshot_res[1], str(snapshot_res[2] or "")
                elif len(snapshot_res) == 2:
                    emotion_scores, emotion_reason = snapshot_res[0], str(snapshot_res[1] or "")
        except Exception as e:
            log.warning("get_emotion_snapshot failed: %s", e)
    emotion_scores = merge_emotion_scores(emotion_scores, incoming_emotion_scores)

    # 4. ユーザー識別情報の安全な解決
    guild_id = None
    if message is not None and getattr(message, "guild", None):
        guild_id = getattr(message.guild, "id", None)
    if guild_id is None and runtime is not None:
        guild_id = getattr(runtime, "guild_id", None)

    uid = None
    if message is not None:
        uid = author_id(message)
    if uid is None and runtime is not None:
        uid = getattr(runtime, "author_id", None) or getattr(runtime, "user_id", None)

    raw_user_name = ""
    if message is not None and getattr(message, "author", None):
        raw_user_name = getattr(message.author, "name", "")
    if not raw_user_name and runtime is not None:
        raw_user_name = getattr(runtime, "author_name", "") or getattr(runtime, "user_name", "")

    user_disp_name = (
        getattr(runtime, "author_display_name", "")
        or (display_name(message) if message is not None else "")
        or getattr(runtime, "author_name", "")
        or raw_user_name
    )

    channel_id = getattr(runtime, "cid", None) if runtime is not None else None
    user_text = getattr(runtime, "original_user_text", "") if runtime is not None else ""

    # 5. 長期記憶の抽出と保存
    extraction_result: dict[str, Any] | None = None
    if not skip_long_term_memory:
        try:
            extraction_result = await actual_extract_memory_fn(
                cfg=cfg_fn,
                store=store,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=uid,
                user_text=user_text,
                assistant_text=assistant_text,
                context_lines=memory_context_lines,
                emotion_scores=emotion_scores,
                emotion_reason=emotion_reason,
                user_name=raw_user_name,
                user_display_name=user_disp_name,
            )
        except Exception as e:
            log.exception("memory extract failed: %s", e)

    # 6. エピソード記憶の保存（自立実装 maybe_store_episode_memory を直接呼び出し）
    try:
        await maybe_store_episode_memory(
            store,
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

    # 7. ディープリフレクション（高感情時の自発内省タスク）
    highest_emotion_score = max(emotion_scores.values()) if emotion_scores else 0.0
    if highest_emotion_score >= 0.8:
        reflection_func = deep_reflection_fn or getattr(bot_cog, "_run_deep_reflection_once", None)
        if callable(reflection_func):
            _reflection_task = asyncio.create_task(reflection_func(
                channel_id=channel_id,
                emotion_scores=emotion_scores,
                emotion_reason=emotion_reason or "不明",
            ))
            _reflection_task.add_done_callback(
                lambda t: log.warning("deep reflection task failed: %r", t.exception()) if t.exception() else None
            )

    # 8. 学習候補保存（ハルシネーション検出時は除外）
    candidate_saved = False
    if not skip_long_term_memory:
        try:
            has_hallucinated = bool(extraction_result.get("has_hallucinated_memory")) if isinstance(extraction_result, dict) else False
            candidate_saved = await actual_store_candidate_fn(
                cfg=cfg_fn,
                store=store,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=uid,
                user_text=user_text,
                assistant_text=assistant_text,
                context_lines=trimmed_context_lines,
                looks_like_reasoning_leak=looks_like_reasoning_leak,
                looks_like_parrot_reply=looks_like_parrot_reply,
                is_weather_reply=getattr(runtime, "is_weather_reply", False) if runtime is not None else False,
                has_hallucinated_memory=has_hallucinated,
            )
        except Exception as e:
            log.exception("training candidate save failed: %s", e)
            candidate_saved = False

    # 9. 習慣プロファイルの更新
    if candidate_saved:
        try:
            await actual_update_habit_fn(
                cfg=cfg_fn,
                store=store,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=uid,
                user_text=user_text,
                assistant_text=assistant_text,
                context_lines=memory_context_lines,
                candidate_reason="auto-saved training candidate",
                user_name=raw_user_name,
                user_display_name=user_disp_name,
            )
        except Exception as e:
            log.exception("habit profile update failed: %s", e)

    # 10. ユーザー関心事の更新
    if not skip_long_term_memory:
        try:
            await actual_update_interests_fn(
                cfg=cfg_fn,
                store=store,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=uid,
                user_text=user_text,
                context_lines=memory_context_lines,
            )
        except Exception as e:
            log.exception("user interests update failed: %s", e)

    # 11. チャンネル会話要約の更新
    summary_builder = (
        build_summary_lines_fn
        or getattr(bot_cog, "_build_summary_context_lines", None)
    )
    try:
        if callable(summary_builder):
            summary_res = summary_builder(
                message,
                sent,
                base_context_lines=memory_context_lines,
                bot_user_id=getattr(runtime, "bot_user_id", None) if runtime is not None else None,
                fetch_limit=max(
                    int(cfg_fn("MEMORY_SUMMARY_CONTEXT_LINES", cfg_fn("MEMORY_LONG_CONTEXT_LINES", 30)) or 30),
                    int(cfg_fn("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20) or 20),
                ),
            )
            if inspect.isawaitable(summary_res):
                summary_context_lines = await summary_res
            elif isinstance(summary_res, list):
                summary_context_lines = summary_res
            else:
                summary_context_lines = memory_context_lines
        else:
            active_cache = channel_context_cache or getattr(bot_cog, "channel_context_cache", None)
            if active_cache and channel_id is not None:
                cached_lines_fn = getattr(active_cache, "get_recent_lines", None)
                if callable(cached_lines_fn):
                    summary_context_lines = cached_lines_fn(channel_id) or memory_context_lines
                else:
                    summary_context_lines = memory_context_lines
            else:
                summary_context_lines = memory_context_lines
    except Exception as e:
        log.exception("summary context build failed: %s", e)
        summary_context_lines = memory_context_lines

    try:
        await actual_update_summary_fn(
            cfg=cfg_fn,
            store=store,
            guild_id=guild_id,
            channel_id=channel_id,
            context_lines=summary_context_lines,
        )
    except Exception as e:
        log.exception("channel summary update failed: %s", e)

    # 12. 未来プロンプト完了マーク
    prompts_marker = mark_prompts_fn or getattr(bot_cog, "_mark_future_prompts_triggered", None)
    if callable(prompts_marker) and runtime is not None:
        try:
            marker_res = prompts_marker(runtime)
            if inspect.isawaitable(marker_res):
                await marker_res
        except Exception as e:
            log.exception("prospective trigger update failed: %s", e)

    # 13. 会話スレッド状態を長期記憶DBへアトミック永続化
    active_tracker = thread_tracker or getattr(bot_cog, "thread_tracker", None)
    if active_tracker and channel_id is not None:
        save_thread = getattr(store, "save_channel_thread", None)
        if callable(save_thread):
            try:
                persona_ns = cfg_persona_namespace(custom_cfg=cfg_fn)
                get_active_threads_fn = getattr(active_tracker, "get_active_threads", None)
                active_threads = get_active_threads_fn(channel_id) if callable(get_active_threads_fn) else []
                for th in active_threads:
                    await save_thread(
                        persona=persona_ns,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        thread_key=getattr(th, "thread_id", str(th)),
                        topic=getattr(th, "topic", ""),
                        participants=getattr(th, "participant_names", []),
                        turns=list(getattr(th, "turns", [])),
                        last_active_ts=getattr(th, "last_active_ts", None),
                    )
            except Exception as e:
                log.exception("channel threads persistence failed: %s", e)

    # 14. 習慣ターンの保留記憶
    if not skip_long_term_memory:
        habit_rememberer = remember_habit_fn or getattr(bot_cog, "_remember_pending_habit_turn", None)
        if callable(habit_rememberer) and runtime is not None:
            try:
                habit_res = habit_rememberer(runtime, incoming_emotion_scores=incoming_emotion_scores)
                if inspect.isawaitable(habit_res):
                    await habit_res
            except Exception as e:
                log.exception("remember pending habit turn failed: %s", e)
