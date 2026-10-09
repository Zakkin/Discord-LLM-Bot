"""
[AI Agent Summary]
ユーザー習慣プロファイル更新、学習候補データ保存、チャンネル要約更新を担当するモジュール。
This module handles user habit profile updates, training candidate persistence, and channel summaries.
"""
from __future__ import annotations

import logging
from typing import Any

from .compat import get_ollama_helpers
from .store import MemoryStore
from .extractor import (
    _is_fallback_reply,
    _is_ignorable_call_error,
    _join_context_lines,
    _persona_from_cfg,
)
from ollama_bot.common import ollama_helpers

log = logging.getLogger("harness.agent.memory_manager.summarizer")

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

HABIT_PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_update": {"type": "boolean"},
        "tone_casual": {"type": "number"},
        "response_detail": {"type": "number"},
        "search_preference": {"type": "number"},
        "joke_receptivity": {"type": "number"},
        "confidence": {"type": "number"},
        "reason": {
            "type": "string",
            "description": "対象ユーザーのサーバーニックネーム（例: 『〇〇』さん）またはユーザーIDを主語として記述した分析理由。抽象的な『ユーザー』単体は禁止",
        },
    },
    "required": ["should_update", "tone_casual", "response_detail", "search_preference", "joke_receptivity", "confidence", "reason"],
    "additionalProperties": False,
}


def _profile_score(value: Any, *, default: float = 0.5) -> float:
    try:
        score = float(value)
    except Exception:
        return default
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _habit_profile_model_name(cfg: Any) -> str:
    return str(
        cfg(
            "HABIT_PROFILE_MODEL",
            cfg(
                "OLLAMA_MIDDLE_MODEL",
                cfg("OLLAMA_CLASSIFIER_MODEL", cfg("MEMORY_EXTRACT_MODEL", cfg("OLLAMA_MODEL", ""))),
            ),
        )
        or ""
    )


async def update_user_habit_profile(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    candidate_reason: str = "auto-saved",
    user_name: str | None = None,
    user_display_name: str | None = None,
) -> dict[str, Any] | None:
    if not cfg("HABIT_PROFILE_ENABLED", True):
        return None

    if user_id is None:
        return None

    user_text = str(user_text or "").strip()
    assistant_text = str(assistant_text or "").strip()
    if not user_text or not assistant_text:
        return None

    helpers = get_ollama_helpers()
    if helpers.looks_like_abnormal_assistant_reply(assistant_text) or _is_fallback_reply(cfg, assistant_text):
        return None

    persona = _persona_from_cfg(cfg)
    model = _habit_profile_model_name(cfg)
    timeout_sec = float(cfg("HABIT_PROFILE_TIMEOUT_SEC", cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0)) or 60.0)
    retries, temperature = int(cfg("HABIT_PROFILE_RETRIES", 1) or 1), float(cfg("HABIT_PROFILE_TEMPERATURE", 0.1) or 0.1)
    min_confidence = _profile_score(cfg("HABIT_PROFILE_MIN_CONFIDENCE", 0.35), default=0.35)
    min_delta, base_alpha = max(0.0, min(0.49, float(cfg("HABIT_PROFILE_SIGNAL_MIN_DELTA", 0.08) or 0.08))), max(0.0, min(1.0, float(cfg("HABIT_PROFILE_EMA_ALPHA", 0.12) or 0.12)))

    target_disp = str(user_display_name or "").strip()
    if not target_disp:
        from .user_identity_resolver import resolve_user_display_name
        target_disp = await resolve_user_display_name(
            user_id,
            guild=None,
            memory_store=store,
            default_name=user_name,
        )

    clean_disp = target_disp
    if clean_disp.startswith(("『", "「")) and clean_disp.endswith(("』", "」")):
        clean_disp = clean_disp[1:-1].strip()
    base_name = clean_disp
    for suffix in ("さん", "くん", "君", "ちゃん", "氏", "様"):
        if base_name.endswith(suffix):
            base_name = base_name[:-len(suffix)].strip()
            break
    user_label = f"『{base_name}』さん" if base_name else f"ユーザー（ID: {user_id}）"

    context_text = _join_context_lines(
        context_lines,
        limit=int(cfg("HABIT_PROFILE_CONTEXT_LINES", 16) or 16),
        max_chars_per_line=int(cfg("HABIT_PROFILE_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("HABIT_PROFILE_CONTEXT_MAX_TOTAL_CHARS", 3200) or 3200),
    )
    system_prompt = (
        "あなたはDiscord会話から、相手ごとの会話習慣プロファイルを数値化する補助AIです。\n"
        f"今回の対話相手は {user_label}（ユーザーID: {user_id}）です。\n"
        "出力はJSONのみです。\n"
        f"【時系列と因果関係の厳守】時系列は【直近文脈】➔【{user_label}の発言】➔【AI返答】の順です。\n"
        f"【AI返答】は【{user_label}の発言】を受けてAIが返したリアクションです。AI返答に対するユーザーの反応として因果関係を前後逆転させて解釈することを固く禁じます。\n"
        "各スコアは0.0から1.0で、0.5は今回の会話からは判断しない中立値です。\n"
        "tone_casual は砕けた口調を好むほど高く、丁寧で落ち着いた口調を好むほど低くします。\n"
        "response_detail は説明を詳しくしてほしいほど高く、短く結論だけを好むほど低くします。\n"
        "search_preference は不明点や新しい話題で外部確認を望むほど高く、雑談テンポ優先ほど低くします。\n"
        "joke_receptivity は軽い冗談やツッコミを受け入れやすいほど高く、真面目な返答を好むほど低くします。\n"
        "明示的な依頼・反応・継続的な文脈から読める時だけ0.5から動かし、単なるbot側の口調を相手の好みと誤認しないでください。\n"
        f"⚠️ 判断理由（reason）には、抽象的な『ユーザー』ではなく必ず対象のサーバーニックネーム（{user_label}）またはユーザーID（ID: {user_id}）を主語・対象として記述してください。"
    )
    prompt = (
        f"[対話相手]\nサーバーニックネーム: {user_label} (ユーザーID: {user_id})\n\n"
        f"【時系列と因果関係の厳守】時系列は【直近文脈】➔【{user_label}の発言】➔【AI返答】の順です。\n"
        f"【AI返答】は【{user_label}の発言】を受けてAIが返したリアクションです。AI返答に対するユーザーの反応として因果関係を前後逆転させて解釈することを固く禁じます。\n\n"
        f"[候補理由]\n{candidate_reason or 'auto-saved'}\n\n"
        f"[直近文脈]\n{context_text or '(なし)'}\n\n"
        f"[{user_label}の発言]\n{helpers.truncate_text(user_text, 600)}\n\n"
        f"[AI返答]\n{helpers.truncate_text(assistant_text, 900)}\n\n"
        f"この1ターンから、今後の{user_label}への返答に戻せる会話習慣があるか判定してください。\n"
        f"判断理由（reason）は必ず {user_label}（またはID: {user_id}）を主語・対象として記述してください（例: 『{user_label}の発言は具体的な商品名の問い合わせであり…』）。\n"
        "判断できない項目は必ず0.5にしてください。"
    )

    try:
        result = await helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=HABIT_PROFILE_SCHEMA,
            model=model,
            temperature=temperature,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        if _is_ignorable_call_error(e):
            log.warning("update_user_habit_profile: call_ollama_json failed: %r", e)
        else:
            log.exception("update_user_habit_profile: call_ollama_json failed")
        return None

    if not isinstance(result, dict) or not bool(result.get("should_update")):
        return None

    # 事後正規化ガード（多重防御）: 「ユーザーの発言は」等の抽象主語を対象サーバーニックネーム・ユーザーIDタグに補正
    raw_reason = str(result.get("reason") or "").strip()
    if raw_reason:
        from .user_identity_resolver import get_user_aliases_from_store
        from .extractor import normalize_memory_text

        aliases: list[str] = []
        try:
            aliases = await get_user_aliases_from_store(
                user_id,
                guild=None,
                memory_store=store,
            )
        except Exception:
            pass

        normalized_reason = normalize_memory_text(
            raw_reason,
            target_user_name=base_name,
            target_user_id=user_id,
            aliases=aliases,
            include_uid_tag=True,
        )
        result["reason"] = normalized_reason

    confidence = _profile_score(result.get("confidence", 0.0), default=0.0)
    if confidence < min_confidence:
        return None

    signals = {
        "tone_casual": _profile_score(result.get("tone_casual"), default=0.5),
        "response_detail": _profile_score(result.get("response_detail"), default=0.5),
        "search_preference": _profile_score(result.get("search_preference"), default=0.5),
        "joke_receptivity": _profile_score(result.get("joke_receptivity"), default=0.5),
    }
    updates: dict[str, float | None] = {
        key: value if abs(value - 0.5) >= min_delta else None
        for key, value in signals.items()
    }
    if not any(value is not None for value in updates.values()):
        return None

    method = getattr(store, "upsert_user_habit_profile", None)
    if not callable(method):
        log.debug("update_user_habit_profile: store has no upsert_user_habit_profile")
        return None

    try:
        return await method(
            persona=persona,
            guild_id=guild_id,
            user_id=user_id,
            tone_casual=updates["tone_casual"],
            response_detail=updates["response_detail"],
            search_preference=updates["search_preference"],
            joke_receptivity=updates["joke_receptivity"],
            alpha=base_alpha * max(confidence, 0.25),
            source_summary=str(result.get("reason") or "").strip(),
            confidence=confidence,
        )
    except Exception:
        log.exception(
            "update_user_habit_profile: profile upsert failed persona=%r guild=%r channel=%r user=%r",
            persona,
            guild_id,
            channel_id,
            user_id,
        )
        return None


async def get_user_habit_profile(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    user_id: int | None,
) -> dict[str, Any] | None:
    if not cfg("HABIT_PROFILE_ENABLED", True) or user_id is None:
        return None
    method = getattr(store, "get_user_habit_profile", None)
    if not callable(method):
        return None
    try:
        return await method(
            persona=_persona_from_cfg(cfg),
            guild_id=guild_id,
            user_id=user_id,
        )
    except Exception:
        log.exception("get_user_habit_profile failed user_id=%r", user_id)
        return None


def format_user_habit_profile_lines(
    profile: dict[str, Any] | None,
    *,
    min_evidence: int = 1,
    min_delta: float = 0.10,
) -> list[str]:
    if not profile or int(profile.get("evidence_count", 0) or 0) < max(int(min_evidence), 1):
        return []
    threshold = max(0.0, min(0.49, float(min_delta)))
    items = [
        ("tone_casual", "口調: 砕けた自然な口調を受け入れやすい。", "口調: 落ち着いた丁寧寄りの口調が合いやすい。"),
        ("response_detail", "説明量: 結論だけでなく理由や手順も少し足すとよい。", "説明量: 短めに、結論や反応を先に置く方が合いやすい。"),
        ("search_preference", "検索: 不明点や新しい話題では外部確認を好む傾向がある。", "検索: 雑談では検索よりテンポを優先する方が合いやすい。"),
        ("joke_receptivity", "冗談: 軽い冗談やツッコミを返してもよい。", "冗談: からかいより素直で穏やかな返答を優先する。"),
    ]
    lines: list[str] = []
    for key, high_msg, low_msg in items:
        val = _profile_score(profile.get(key), default=0.5)
        if val >= 0.5 + threshold:
            lines.append(high_msg)
        elif val <= 0.5 - threshold:
            lines.append(low_msg)
    return lines[:4]


async def get_user_habit_profile_lines(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    user_id: int | None,
) -> list[str]:
    profile = await get_user_habit_profile(
        cfg=cfg,
        store=store,
        guild_id=guild_id,
        user_id=user_id,
    )
    return format_user_habit_profile_lines(
        profile,
        min_evidence=int(cfg("HABIT_PROFILE_PROMPT_MIN_EVIDENCE", 1) or 1),
        min_delta=float(cfg("HABIT_PROFILE_PROMPT_MIN_DELTA", 0.10) or 0.10),
    )


def _extract_last_assistant_text_from_context(context_lines: list[str]) -> str:
    """context_lines の末尾方向から直前の Assistant 発言を抽出する。"""
    import re
    from .extractor import _is_assistant_context_line
    for line in reversed(context_lines or []):
        raw = str(line or "").strip()
        if not raw:
            continue
        if raw.startswith("Assistant:"):
            return raw[len("Assistant:"):].strip()
        if raw.startswith("AI:"):
            return raw[len("AI:"):].strip()
        if raw.startswith("Bot:"):
            return raw[len("Bot:"):].strip()
        if _is_assistant_context_line(raw):
            m = re.match(r"^[^:\n]{1,25}[:：]\s*(.*)$", raw)
            if m:
                return m.group(1).strip()
    return ""


async def maybe_store_training_candidate(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    looks_like_reasoning_leak: Any,
    looks_like_parrot_reply: Any,
    is_weather_reply: bool = False,
    has_hallucinated_memory: bool = False,
) -> bool:
    if not cfg("TRAINING_CANDIDATE_ENABLED", True):
        return False

    if has_hallucinated_memory:
        log.info("maybe_store_training_candidate: skipped turn with hallucinated memory")
        return False

    if not user_text or not assistant_text:
        return False

    if len(assistant_text.strip()) < 6:
        return False

    if is_weather_reply:
        return False

    helpers = get_ollama_helpers()
    if helpers.looks_like_abnormal_assistant_reply(assistant_text) or _is_fallback_reply(cfg, assistant_text):
        return False

    try:
        if looks_like_reasoning_leak(assistant_text):
            return False
    except Exception:
        log.exception("maybe_store_training_candidate: reasoning leak check failed")

    try:
        if looks_like_parrot_reply(user_text, assistant_text):
            return False
    except Exception:
        log.exception("maybe_store_training_candidate: parrot check failed")

    try:
        from harness.agent.prompt_parser.parser import (
            looks_like_abnormal_rejection,
            looks_like_character_deviation,
            looks_like_self_contradicting_reply,
        )
        if looks_like_abnormal_rejection(assistant_text):
            log.info("maybe_store_training_candidate: rejected reply with abnormal rejection")
            return False
        if looks_like_character_deviation(assistant_text, user_text=user_text):
            log.info("maybe_store_training_candidate: rejected reply with character deviation")
            return False
        last_ai_text = _extract_last_assistant_text_from_context(context_lines)
        if last_ai_text and looks_like_self_contradicting_reply(assistant_text, last_assistant_text=last_ai_text, user_text=user_text):
            log.info("maybe_store_training_candidate: rejected self-contradicting reply")
            return False
    except Exception:
        log.exception("maybe_store_training_candidate: quality filter check failed")

    persona = _persona_from_cfg(cfg)
    context_text = _join_context_lines(context_lines, limit=12)

    await store.add_training_candidate(
        persona=persona,
        guild_id=guild_id,
        channel_id=channel_id,
        user_id=user_id,
        user_text=user_text.strip(),
        assistant_text=assistant_text.strip(),
        context_text=context_text,
        reason="auto-saved",
    )
    return True


async def maybe_update_channel_summary(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    context_lines: list[str],
) -> bool:
    if not cfg("MEMORY_ENABLED", True):
        return False

    trigger = int(cfg("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20))
    if channel_id is None:
        return False
    if len(context_lines) < trigger:
        return False

    persona = _persona_from_cfg(cfg)
    model = str(cfg("MEMORY_EXTRACT_MODEL", cfg("OLLAMA_MODEL", "")))
    max_lines = int(cfg("MEMORY_SUMMARY_MAX_LINES", 5))
    timeout_sec = float(cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0))
    retries = int(cfg("MEMORY_EXTRACT_RETRIES", 2))
    temperature = float(cfg("MEMORY_EXTRACT_TEMPERATURE", 0.1))

    system_prompt = (
        "あなたはDiscord会話の内部要約を作る補助AIです。"
        "継続中の話題、重要前提、参加者の意図だけを短く残してください。"
        "雑談のノイズはできるだけ省いてください。"
        "出力はJSONのみで返してください。"
    )

    helpers = get_ollama_helpers()
    cleaned_lines = [
        line for line in context_lines
        if not (
            helpers.looks_like_abnormal_assistant_reply(line.split(": ", 1)[1] if ": " in line else line)
            or _is_fallback_reply(cfg, line.split(": ", 1)[1] if ": " in line else line)
        )
    ]
    summary_context_limit = max(
        int(cfg("MEMORY_SUMMARY_CONTEXT_LINES", cfg("MEMORY_LONG_CONTEXT_LINES", max(trigger, 20))) or max(trigger, 20)),
        trigger,
    )
    lines = helpers.truncate_lines(
        cleaned_lines[-summary_context_limit:],
        max_lines=summary_context_limit,
        max_chars_per_line=int(cfg("MEMORY_SUMMARY_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("MEMORY_SUMMARY_CONTEXT_MAX_TOTAL_CHARS", cfg("MEMORY_LONG_CONTEXT_MAX_TOTAL_CHARS", 5000)) or 5000),
    )
    if not lines:
        return False
    prompt = f"次の会話を内部メモとして最大{max_lines}行程度で要約してください\n\n[会話]\n" + "\n".join(lines)

    try:
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            log.info(
                "maybe_update_channel_summary: model=%s timeout_sec=%s retries=%s context=%r",
                model,
                timeout_sec,
                retries,
                helpers.truncate_text("\n".join(lines), int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
            )
        result = await helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=SUMMARY_SCHEMA,
            model=model,
            num_predict=int(cfg("MEMORY_SUMMARY_NUM_PREDICT", 512) or 512),
            temperature=temperature,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        if _is_ignorable_call_error(e):
            log.warning("maybe_update_channel_summary: call_ollama_json failed: %r", e)
        else:
            log.exception("maybe_update_channel_summary: call_ollama_json failed")
        return False

    summary = str((result or {}).get("summary", "")).strip()
    if not summary:
        return False

    await store.set_channel_summary(
        persona=persona,
        guild_id=guild_id,
        channel_id=int(channel_id),
        summary=summary[:1000],
        source_count=len(lines),
    )
    return True
