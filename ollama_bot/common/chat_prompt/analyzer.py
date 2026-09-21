from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from ..config_helpers import cfg
from ..emotion_helpers import format_emotion_tags_ja
from .. import ollama_helpers
from ..reply_safety import _normalize_compare_text
from ..bot_identity import BotIdentity, is_bot_name_mentioned, resolve_bot_identity
from ...ollama_chat_.ollama_chat_texts import (
    PRE_REPLY_ANALYZER_SYSTEM_PROMPT,
    build_pre_reply_analyzer_system_prompt,
)
from .budget import (
    _clean_analyzer_keyword,
    _clean_analyzer_note,
    _clean_selected_memory_indices,
)

log = logging.getLogger("ollama_bot.common.chat_prompt.analyzer")

_EXTERNAL_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_PRE_REPLY_ANALYZER_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "maxLength": 20},
        "target_is_ai": {"type": "boolean"},
        "should_clarify": {"type": "boolean"},
        "selected_indices": {"type": "array", "items": {"type": "integer"}, "maxItems": 3},
        "emotion_focus": {"type": "string", "maxLength": 24},
        "reply_strategy": {"type": "string", "maxLength": 20},
        "avoid": {"type": "string", "maxLength": 50},
        "persona_risk": {"type": "boolean"},
        "consistency_correction": {"type": "string", "maxLength": 50},
        "is_breakdown": {"type": "boolean"},
        "breakdown_reason": {"type": "string", "maxLength": 60},
        "uncertainty_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "needs_clarification": {"type": "boolean"},
    },
    "required": [
        "intent",
        "target_is_ai",
        "should_clarify",
        "selected_indices",
        "emotion_focus",
        "reply_strategy",
        "avoid",
        "persona_risk",
        "consistency_correction",
        "is_breakdown",
        "breakdown_reason",
        "uncertainty_level",
        "needs_clarification",
    ],
    "additionalProperties": False,
}


def _classifier_model_name() -> str:
    return cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))


def _looks_like_x_timeline_research_request(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    compacted = re.sub(r"\s+", "", raw).lower()
    has_x_reference = bool(
        re.search(r"(?<![a-z0-9])(?:x|twitter)(?![a-z0-9])", raw, flags=re.IGNORECASE)
        or "ｘ" in compacted
        or "ツイッター" in compacted
        or "旧twitter" in compacted
        or "旧ツイッター" in compacted
    )
    has_timeline_reference = bool(
        re.search(r"(?<![a-z0-9])(?:tl|timeline|feed)(?![a-z0-9])", raw, flags=re.IGNORECASE)
        or "タイムライン" in compacted
        or "ホームtl" in compacted
        or "ホームフィード" in compacted
        or "フィード" in compacted
    )
    home_reference = "ホーム" in compacted and "ホームページ" not in compacted
    live_or_action = any(
        marker in compacted
        for marker in (
            "今",
            "いま",
            "現在",
            "最新",
            "直近",
            "流れて",
            "流れてる",
            "流れている",
            "どうなって",
            "見て",
            "みて",
            "拾って",
            "取得",
            "確認",
            "教えて",
            "教えろ",
        )
    )
    explanation_only = any(
        marker in compacted
        for marker in ("タイムラインとは", "tlとは", "timelineとは", "タイムラインの意味", "tlの意味")
    ) and not live_or_action
    direct_timeline_request = has_timeline_reference and live_or_action
    x_flow_request = has_x_reference and any(
        marker in compacted for marker in ("流れて", "流れてる", "流れている", "何が", "なにが", "どうなって")
    )
    return bool(
        not explanation_only
        and (
            (has_x_reference and (has_timeline_reference or home_reference))
            or direct_timeline_request
            or x_flow_request
        )
    )


def _looks_like_external_research_context_request(text: str) -> bool:
    raw = str(text or "")
    return bool(_EXTERNAL_URL_RE.search(raw) or _looks_like_x_timeline_research_request(raw))


_STATIC_NON_THIRD_PARTY_SUBJECTS = {
    # AI自身・二人称（Bot宛て言及）
    "bot", "ai", "ボット", "あなた", "あんた", "お前", "君", "きみ", "そっち", "おまえ",
    "assistant", "アシスタント",
    # ユーザー自身の一人称（話者自身の過去発言）
    "俺", "おれ", "オレ", "私", "わたし", "ワタシ", "僕", "ぼく", "ボク", "自分", "うち", "わし", "ワイ", "小生", "我",
}
_NON_THIRD_PARTY_SUBJECTS = _STATIC_NON_THIRD_PARTY_SUBJECTS


def _get_non_third_party_subjects() -> set[str]:
    subjects = set(_STATIC_NON_THIRD_PARTY_SUBJECTS)
    aliases = cfg("BOT_ALIASES", ())
    if isinstance(aliases, (list, tuple, set)):
        subjects.update(str(a).strip().lower() for a in aliases if a)
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip().lower()
    if first_person:
        subjects.add(first_person)
    return subjects


_HEARSAY_EXTRACT_RE = re.compile(
    r"(?:^|[\n\r、。\s]+)"
    r"(?:そういえば|ちなみに|あのさ|そうそう|あと)?\s*"
    r"([^\s　、。:：]{1,24}?)"
    r"(?:は|が|って|曰く|によると|によれば|いわく)"
    r"(.+?)"
    r"(?:と言ってた|って言ってた|と言っていた|って言っていた|と話してた|と話していた|らしい|ってさ|ってよ)"
    r"(?:ぞ|ぜ|よ|ね|な|わ|さ|か|！|!|？|\?|。|\.|\s)*$",
    re.DOTALL,
)


def detect_third_party_hearsay(
    text: str,
    *,
    context_lines: list[str] | None = None,
) -> tuple[bool, str, str]:
    """
    発言が『〇〇は〜と言ってたぞ』『〇〇さんが〜って言っていた』等の第三者の発言・伝聞の報告であるかを判定する。
    AI自身への直接の指摘や、話者自身の一人称による発言は除外する。
    戻り値: (is_hearsay, subject_name, reported_content)
    """
    raw = str(text or "").strip()
    if not raw:
        return False, "", ""

    match = _HEARSAY_EXTRACT_RE.search(raw)
    if not match:
        return False, "", ""

    subject = match.group(1).strip()
    content_str = match.group(2).strip()

    lowered_subject = subject.lower()
    base_subject = re.sub(r"(さん|くん|君|ちゃん|氏|先生)$", "", lowered_subject).strip()

    if not base_subject:
        return False, "", ""

    non_third_party = _get_non_third_party_subjects()
    if base_subject in non_third_party or lowered_subject in non_third_party:
        return False, "", ""

    if not content_str:
        return False, "", ""

    return True, subject, content_str


def _should_skip_intent_analysis(*, current_user_text: str, last_assistant_text: str) -> bool:
    text = (current_user_text or "").strip()
    if not text:
        return True

    compact = "".join(text.split())
    short_threshold = int(cfg("OLLAMA_INTENT_SKIP_MAX_CHARS", 120) or 120)
    if len(compact) > short_threshold:
        return False

    if "\n" in text:
        return False
    if any(ch in text for ch in ("?", "？")):
        return False

    easy_markers = (
        "おはよ", "こんにちは", "こんばんは", "ただいま", "おやすみ",
        "眠い", "ねむ", "疲れ", "つかれ", "しんど", "だる", "暇",
        "ありがとう", "草", "w", "笑", "なんで", "ほんと", "マジ", "まじ",
        "了解", "りょ", "うける", "やば", "かわいい", "可愛い", "かわよ",
        "それな", "たすかる", "助かる", "えらい", "すごい", "すげ", "うけた",
        "して", "やって", "見て", "教えて", "どう", "何これ", "これ", "それ",
    )
    if any(marker in text for marker in easy_markers):
        return True

    if last_assistant_text and len(compact) <= 40:
        return True

    return False


def _infer_user_intent_without_llm(
    *,
    current_user_text: str,
    last_assistant_text: str,
    is_reply_to_other_user: bool = False,
    bot_identity: BotIdentity | None = None,
) -> dict[str, Any]:
    text = str(current_user_text or "").strip()
    if not text:
        return {
            "intent": "",
            "target_is_ai": False,
            "should_clarify": True,
            "reason": "heuristic_empty",
        }

    compact = re.sub(r"\s+", "", text)
    lowered = compact.lower()

    if re.search(r"(おはよ|こんにちは|こんばんは|ただいま|おやすみ)", text):
        return {
            "intent": "greeting",
            "target_is_ai": False,
            "should_clarify": False,
            "reason": "heuristic_greeting",
        }

    if re.search(r"(眠い|ねむ|疲れ|つかれ|しんど|だる|暇|うれし|嬉し|かなしい|悲し|寂し|さみし|腹減|おなかすいた)", text):
        return {
            "intent": "monologue",
            "target_is_ai": False,
            "should_clarify": False,
            "reason": "heuristic_monologue",
        }

    direct_request = bool(re.search(
        r"(してくれ|してよ|して\?|して？|して$|して。|して！|して!|やって|教えて|教えろ|見せて|答えて|話して|要約して|調べて|解説して|どうする|どう思う|できる|くれる|ほしい)",
        text,
    ))
    looks_like_question = bool(re.search(r"[?？]|\bwhat\b|\bwhy\b|\bhow\b", lowered))

    is_hearsay, hearsay_subject, hearsay_content = detect_third_party_hearsay(text)
    if is_hearsay and not direct_request:
        return {
            "intent": "状況報告",
            "target_is_ai": False,
            "should_clarify": False,
            "reason": "heuristic_third_party_hearsay",
            "hearsay_subject": hearsay_subject,
            "hearsay_content": hearsay_content,
        }

    bot_name_in_text = is_bot_name_mentioned(text, bot_identity)

    if is_reply_to_other_user:
        explicit_bot_mention = bot_name_in_text or bool(re.search(
            r"(bot|ボット|ai)",
            lowered,
        ))
        if explicit_bot_mention and direct_request:
            return {
                "intent": "request",
                "target_is_ai": True,
                "should_clarify": False,
                "reason": "heuristic_explicit_bot_request_in_reply",
            }
        if explicit_bot_mention and looks_like_question:
            return {
                "intent": "question",
                "target_is_ai": True,
                "should_clarify": False,
                "reason": "heuristic_explicit_bot_question_in_reply",
            }
        return {
            "intent": "statement",
            "target_is_ai": False,
            "should_clarify": False,
            "reason": "heuristic_reply_to_other_user",
        }

    ai_status_mention = bool(re.search(
        r"(怒ってる|おこってる|怒った|怒り|ブチギレ|泣いてる|泣いた|笑ってる|笑った|機嫌|ツンデレ|デレた|照れてる|ビビってる|怖がってる|bot|ボット|AI|お前|君|あんた|あなた)",
        text,
    )) or bot_name_in_text

    if direct_request or looks_like_question or ai_status_mention:
        return {
            "intent": "request" if direct_request else ("question" if looks_like_question else "statement"),
            "target_is_ai": True,
            "should_clarify": False,
            "reason": "heuristic_direct_question_or_mention",
        }

    if len(compact) <= 4 and re.fullmatch(r"(これ|それ|あれ|どれ|え|ん|は\?|は？|それで)", compact):
        return {
            "intent": "unclear",
            "target_is_ai": bool(last_assistant_text),
            "should_clarify": True,
            "reason": "heuristic_too_short",
        }

    return {
        "intent": "statement",
        "target_is_ai": False,
        "should_clarify": False,
        "reason": "heuristic_default",
    }


def _should_skip_memory_selection(
    *,
    current_user_text: str,
    last_assistant_text: str,
    memory_lines: list[str],
    channel_summary: str | None,
    preferred_emotion_tags: list[str] | None = None,
) -> bool:
    text = (current_user_text or "").strip()
    if not text:
        return True
    if re.fullmatch(r"（[^（）\n]{1,120}への返信）", text):
        return True
    if not memory_lines and not channel_summary:
        return True

    effective_text = re.sub(r"^（[^（）\n]{1,120}への返信）[\s\n]*", "", text).strip()
    if not effective_text:
        return True

    compact = "".join(effective_text.split())
    short_threshold = int(cfg("OLLAMA_MEMORY_SELECTION_SKIP_MAX_CHARS", 90) or 90)
    if len(compact) > short_threshold:
        return False

    if any(token in effective_text for token in ("?", "？", "http://", "https://")):
        return False
    if "\n" in effective_text:
        return False

    casual_markers = (
        "おはよ", "こんにちは", "こんばんは", "ただいま", "おやすみ",
        "眠い", "ねむ", "疲れ", "つかれ", "しんど", "だる", "暇",
        "ありがとう", "草", "w", "笑", "なんで", "ほんと", "マジ", "まじ",
        "了解", "りょ", "うける", "やば", "かわいい", "可愛い", "かわよ",
        "それな", "たすかる", "助かる", "えらい", "すごい", "すげ", "うけた",
        "昼ごはん", "昼ご飯", "昼飯", "昼食", "ランチ", "朝ごはん", "朝ご飯",
        "朝飯", "晩ごはん", "晩ご飯", "晩飯", "夕飯", "夜ごはん", "夜ご飯",
        "何食べ", "食おう", "食べよう", "食べよ", "腹減", "おなかすいた", "お腹すいた",
        "消せ", "やめろ", "やめて", "ダメ", "だめ", "無理", "むり",
        "なるほど", "たしかに", "確かに", "そうなんだ", "そうなん",
        "うん", "はい", "そうそう", "へえ", "ほーん",
    )
    if any(marker in effective_text for marker in casual_markers):
        return True

    emotion_hook_active = any(str(tag).strip() for tag in (preferred_emotion_tags or []))
    if emotion_hook_active:
        if len(compact) <= 15 and not any(token in effective_text for token in ("?", "？", "誰", "何")):
            return True
        return "http://" in effective_text or "https://" in effective_text

    return False


def _filter_memories_matching_user_text(
    memories: list[str],
    user_text: str,
    context_text: str = "",
) -> list[str]:
    """ユーザー発言（および会話文脈）に明示的に含まれるキーワードと一致する記憶のみを抽出する。"""
    clean_text = _EXTERNAL_URL_RE.sub("", f"{user_text or ''}\n{context_text or ''}".strip())
    tokens = set(
        re.findall(
            r"[A-Za-z0-9_]{2,}|[\u30A0-\u30FFー]{2,}|[\u4E00-\u9FFF]{2,}|[\u4E00-\u9FFF][\u3040-\u309F]{1,4}",
            clean_text,
        )
    )
    stop_tokens = {
        "これ", "それ", "あれ", "どれ", "見て", "みて", "どう", "なに", "何",
        "この", "その", "あの", "どの", "よう", "こと", "もの", "ため",
        "http", "https", "com", "net", "org", "www", "live", "app",
        "覚えて", "おぼえて", "記憶して", "ください",
    }
    meaningful_tokens = {t for t in tokens if t.lower() not in stop_tokens and len(t) >= 2}
    if not meaningful_tokens:
        return []

    matched: list[str] = []
    for mem in memories:
        if any(token in mem for token in meaningful_tokens):
            matched.append(mem)
            continue
        subject = mem.split(":", 1)[0].split("：", 1)[0].strip()
        subject = re.sub(r"^\[[^\]]+\]\s*", "", subject).strip()
        if len(subject) >= 2 and subject in clean_text:
            matched.append(mem)

    return matched


def _post_filter_selected_memories(
    selected_memories: list[str],
    user_text: str,
    last_assistant_text: str = "",
) -> list[str]:
    """Middle LLMが選定した記憶から、教示文等で選ばれた無関係な記憶（全選択ハルシネーション等）を除外する。"""
    if not selected_memories:
        return []

    from ..web_research.helpers import looks_like_user_teaching_or_definition

    is_teaching = looks_like_user_teaching_or_definition(user_text)
    if is_teaching:
        # 教示・定義・記憶依頼文（「玉ちゃんはホモです。覚えてください。」等）の場合：
        # ユーザーは新しい知識・設定をBotに教えている最中なので、
        # 教示内容のキーワードと直接一致しない無関係な過去記憶（ワス等）はすべて除外する
        clean_user = _EXTERNAL_URL_RE.sub("", user_text or "").strip()
        matched = _filter_memories_matching_user_text(selected_memories, clean_user, last_assistant_text)
        if not matched:
            log.info(
                "Filtered out irrelevant memories during user teaching statement: %s",
                selected_memories,
            )
        return matched

    # 通常の雑談や質問では、LLMの意味的な連想（パチンコ ⇔ 賭け話等）を尊重する
    return selected_memories


def _trim_pre_reply_analyzer_inputs(
    *,
    current_user_text: str,
    last_assistant_text: str,
    memory_lines: list[str],
) -> tuple[str, str, list[str]]:
    trimmed_user_text = ollama_helpers.truncate_text(
        current_user_text or "",
        max(int(cfg("OLLAMA_INTENT_ANALYZER_USER_MAX_CHARS", 260) or 260), 80),
    )
    trimmed_last_assistant_text = ollama_helpers.truncate_text(
        last_assistant_text or "",
        max(int(cfg("OLLAMA_INTENT_ANALYZER_LAST_ASSISTANT_MAX_CHARS", 180) or 180), 60),
    )
    trimmed_memory_lines = ollama_helpers.truncate_lines(
        memory_lines,
        max_lines=max(int(cfg("OLLAMA_INTENT_ANALYZER_MEMORY_MAX_LINES", 4) or 4), 1),
        max_chars_per_line=max(int(cfg("OLLAMA_INTENT_ANALYZER_MEMORY_MAX_CHARS_PER_LINE", 180) or 180), 80),
        max_total_chars=max(int(cfg("OLLAMA_INTENT_ANALYZER_MEMORY_MAX_TOTAL_CHARS", 520) or 520), 160),
    )
    return trimmed_user_text, trimmed_last_assistant_text, trimmed_memory_lines


async def _analyze_pre_reply_context(
    *,
    current_user_text: str,
    last_assistant_text: str,
    memory_lines: list[str],
    channel_summary: str | None,
    preferred_emotion_tags: list[str] | None = None,
    is_reply_to_other_user: bool = False,
    reply_context_hint: str | None = None,
    recent_bot_utterances: list[str] | None = None,
    bot_identity: BotIdentity | None = None,
    mentioned_users: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str], str | None, dict[str, Any]]:
    if bot_identity is None:
        bot_identity = resolve_bot_identity()

    intent_info = _infer_user_intent_without_llm(
        current_user_text=current_user_text,
        last_assistant_text=last_assistant_text,
        is_reply_to_other_user=is_reply_to_other_user,
        bot_identity=bot_identity,
    )
    selected_lines: list[str] = []
    deliberation_info: dict[str, Any] = {
        "emotion_focus": "",
        "reply_strategy": "direct_response",
        "avoid": "",
        "persona_risk": False,
        "consistency_correction": "",
        "is_breakdown": False,
        "breakdown_reason": "",
        "uncertainty_level": "low",
        "needs_clarification": False,
    }

    external_research_request = _looks_like_external_research_context_request(current_user_text)
    if external_research_request:
        intent_info = {
            **intent_info,
            "intent": "request",
            "target_is_ai": True,
            "should_clarify": False,
            "reason": "heuristic_external_research_request",
        }

    skip_intent = _should_skip_intent_analysis(current_user_text=current_user_text, last_assistant_text=last_assistant_text)
    skip_memory = _should_skip_memory_selection(
        current_user_text=current_user_text,
        last_assistant_text=last_assistant_text,
        memory_lines=memory_lines,
        channel_summary=channel_summary,
        preferred_emotion_tags=preferred_emotion_tags,
    )
    if external_research_request:
        skip_intent = True
        skip_memory = True

    trimmed_user_text, trimmed_last_assistant_text, analyzer_memory_lines = _trim_pre_reply_analyzer_inputs(
        current_user_text=current_user_text,
        last_assistant_text=last_assistant_text,
        memory_lines=[] if (skip_memory and not external_research_request) else memory_lines,
    )

    if skip_intent and skip_memory:
        text_for_check = (current_user_text or "").strip()
        is_casual = False
        casual_markers = (
            "おはよ", "こんにちは", "こんばんは", "ただいま", "おやすみ",
            "ありがとう", "草", "w", "笑", "なんで", "ほんと", "マジ", "まじ",
            "了解", "りょ", "うける", "やば", "かわいい", "可愛い", "かわよ",
            "それな", "たすかる", "助かる", "えらい", "すごい", "すげ", "うけた",
            "なるほど", "たしかに", "確かに", "そうなんだ", "そうなん",
            "うん", "はい", "そうそう", "へえ", "ほーん",
        )
        if not any(token in text_for_check for token in ("?", "？", "http://", "https://")):
            if len(text_for_check) <= 15 and any(m in text_for_check for m in casual_markers):
                is_casual = True
            elif len(text_for_check) <= 5:
                is_casual = True

        if is_casual:
            selected_lines = []
        elif external_research_request:
            selected_lines = _filter_memories_matching_user_text(analyzer_memory_lines, text_for_check)[:3]
        else:
            selected_lines = analyzer_memory_lines[:3]

        return intent_info, selected_lines, None if external_research_request else channel_summary, deliberation_info

    emotion_hook_text = format_emotion_tags_ja(preferred_emotion_tags)
    indexed_memories = [{"index": i, "text": m} for i, m in enumerate(analyzer_memory_lines)]

    is_replying_to_old_message = "への返信）" in current_user_text
    if is_replying_to_old_message and trimmed_last_assistant_text:
        match = re.search(r"（.+?の「(.+)」への返信）", current_user_text)
        if match:
            ref_text = match.group(1).strip()
            if _normalize_compare_text(ref_text) in _normalize_compare_text(trimmed_last_assistant_text):
                is_replying_to_old_message = False

    prompt_parts = [
        f"直前のAI発言: {trimmed_last_assistant_text if not is_replying_to_old_message else '(古いメッセージへの返信のため省略)'}",
    ]
    if recent_bot_utterances and not is_replying_to_old_message and not is_reply_to_other_user:
        cleaned_history = [
            ollama_helpers.truncate_text(u.replace("\n", " "), 100)
            for u in recent_bot_utterances[:3]
            if u and u.strip()
        ]
        if cleaned_history:
            prompt_parts.append("直近のAI発言履歴:\n" + "\n".join(f"- {h}" for h in cleaned_history))
    if reply_context_hint:
        prompt_parts.append(f"返信の前提文脈: {reply_context_hint}")
    if mentioned_users:
        disp_names = [
            str(u.get("current_display_name") or u.get("current_name") or "").strip()
            for u in mentioned_users
            if str(u.get("current_display_name") or u.get("current_name") or "").strip()
        ]
        if disp_names:
            prompt_parts.append(
                f"言及されたサーバーメンバー: 『{'』、『'.join(disp_names)}』さん（※話題の対象であり、現在の対話相手ではありません）"
            )
    prompt_parts.extend([
        f"最新のユーザー発言: {trimmed_user_text}",
        f"現在の感情フック: {emotion_hook_text or '(なし)'}",
        "候補メモリ一覧:"
    ])

    core_values_str = str(cfg("BOT_CORE_VALUES", "") or "").strip()
    if core_values_str:
        prompt_parts.insert(0, f"キャラクターの核となる価値観・性格: {core_values_str}")

    prompt_parts.extend([f"[{m['index']}] {m['text']}" for m in indexed_memories] if indexed_memories else ["(候補なし)"])

    try:
        model_name = str(cfg("OLLAMA_MIDDLE_MODEL", _classifier_model_name()) or _classifier_model_name())
        system_prompt = build_pre_reply_analyzer_system_prompt(bot_identity)
        result = await ollama_helpers.call_ollama_json(
            "\n".join(prompt_parts),
            system_prompt=system_prompt,
            schema=_PRE_REPLY_ANALYZER_SCHEMA,
            think=False,
            model=model_name,
            retries=max(int(cfg("OLLAMA_INTENT_ANALYZER_RETRIES", 1) or 1), 0),
            temperature=0.0,
            num_predict=max(int(cfg("OLLAMA_INTENT_ANALYZER_NUM_PREDICT", 640) or 640), 384),
        )

        allow_model_intent_override = (not skip_intent) or intent_info.get("reason") == "heuristic_default"
        if allow_model_intent_override:
            raw_intent = _clean_analyzer_keyword(result.get("intent"), fallback="", max_chars=20)
            if raw_intent.lower() in ("fact_check", "factcheck", "fact-check", "research") and not any(
                kw in current_user_text.lower() for kw in ["本当", "ほんと", "ウソ", "うそ", "嘘", "デマ", "マジ", "まじ", "事実", "ファクトチェック", "調べて", "調査", "確認して", "検証"]
            ):
                raw_intent = "質問" if any(q in current_user_text for q in ["？", "?", "何", "どう", "は"]) else "雑談"
            if raw_intent:
                intent_info["intent"] = raw_intent
            model_target_is_ai = bool(result.get("target_is_ai"))
            bot_mentioned = is_bot_name_mentioned(current_user_text, bot_identity)
            # 発言内にBot自身の名前・愛称が含まれており、第三者伝聞（チクリ）でない場合は target_is_ai を True に保護
            if bot_mentioned and intent_info.get("reason") != "heuristic_third_party_hearsay":
                intent_info["target_is_ai"] = True
            else:
                intent_info["target_is_ai"] = model_target_is_ai
            intent_info["should_clarify"] = bool(result.get("should_clarify"))

        if skip_memory:
            selected_lines = []
        else:
            selected_indices = _clean_selected_memory_indices(
                result.get("selected_indices"),
                memory_count=len(analyzer_memory_lines),
            )
            filtered_lines: list[str] = []
            user_text_lower = current_user_text.lower()
            fact_check_request = any(
                kw in user_text_lower
                for kw in ["本当", "ほんと", "ウソ", "うそ", "嘘", "デマ", "マジ", "まじ", "事実", "ファクトチェック", "調べて", "調査", "確認して", "検証"]
            )
            for idx in selected_indices:
                line = analyzer_memory_lines[idx]
                is_special_fact_memory = "以前ファクトチェックした話題" in line or "以前Web調査した話題" in line
                if is_special_fact_memory and not fact_check_request:
                    log.info("Filtered out irrelevant fact_check/research memory: %s", line[:60])
                    continue
                filtered_lines.append(line)
            selected_lines = _post_filter_selected_memories(
                filtered_lines[:3],
                current_user_text,
                last_assistant_text,
            )

        if external_research_request:
            matched = _filter_memories_matching_user_text(selected_lines or analyzer_memory_lines, current_user_text)
            selected_lines = matched[:3]
            channel_summary = None

        deliberation_info["emotion_focus"] = _clean_analyzer_keyword(result.get("emotion_focus"), fallback="", max_chars=24)
        deliberation_info["reply_strategy"] = _clean_analyzer_keyword(
            result.get("reply_strategy"),
            fallback="direct_response",
            max_chars=20,
        )
        deliberation_info["avoid"] = _clean_analyzer_note(result.get("avoid"), max_chars=50)
        raw_persona_risk = bool(result.get("persona_risk"))
        consistency_correction = _clean_analyzer_note(result.get("consistency_correction"), max_chars=50)

        if not raw_persona_risk:
            consistency_correction = ""

        _persona_forbidden_keywords = [
            "について", "話す", "伝える", "事実", "情報", "教える", "答える",
            "ユーザー", "相手", "ai", "bot", "呼ん", "呼ばれ", "認識",
            "場合", "なら", "設定", "質問", "返答", "メッセージ", "言っ", "いう", "こと", "する", "ため",
            "バグ", "報告", "表示", "アプデ", "アップデート", "ファクトチェック", "調査", "結論",
            "話題", "テーマ", "フシギ族",
        ]
        lowered_cc = consistency_correction.lower()
        if consistency_correction and (
            len(consistency_correction) > 30
            or any(kw in lowered_cc for kw in _persona_forbidden_keywords)
        ):
            consistency_correction = ""

        deliberation_info["persona_risk"] = raw_persona_risk and bool(consistency_correction)
        deliberation_info["consistency_correction"] = consistency_correction if deliberation_info["persona_risk"] else ""
        raw_bd_reason = _clean_analyzer_note(result.get("breakdown_reason"), max_chars=60)
        _bd_noop = {"なし", "なし。", "none", "n/a", "-", ""}
        breakdown_reason = "" if raw_bd_reason.lower().strip("。") in _bd_noop else raw_bd_reason
        deliberation_info["is_breakdown"] = bool(result.get("is_breakdown")) and bool(breakdown_reason)
        deliberation_info["breakdown_reason"] = (
            breakdown_reason if deliberation_info["is_breakdown"] else ""
        )
        _unc_map = {"低": "low", "中": "medium", "高": "high", "低い": "low", "中程度": "medium", "高い": "high"}
        raw_unc = str(result.get("uncertainty_level") or "low").strip()
        deliberation_info["uncertainty_level"] = _unc_map.get(raw_unc, raw_unc if raw_unc in ("low", "medium", "high") else "low")
        model_needs_clarification = bool(result.get("needs_clarification"))
        deliberation_info["needs_clarification"] = (
            model_needs_clarification if allow_model_intent_override else bool(intent_info.get("should_clarify"))
        )
        if mentioned_users and (deliberation_info["needs_clarification"] or deliberation_info["uncertainty_level"] == "high"):
            # サーバーメンバーについての質問・言及がある場合、未知語としての聞き返し要求・不確実性を抑制
            deliberation_info["needs_clarification"] = False
            intent_info["should_clarify"] = False
            if deliberation_info["uncertainty_level"] == "high":
                deliberation_info["uncertainty_level"] = "low"


        # 候補メモリの内容が intent, consistency_correction, breakdown_reason に引き写されている場合の汚染サニタイズ
        memory_snippets = [
            re.sub(r"\[\d+\]\s*(\[[^\]]+\]\s*)*", "", m).strip().lower()
            for m in analyzer_memory_lines
            if m and m.strip()
        ]
        if memory_snippets:
            def _is_polluted(target_text: str) -> bool:
                t = (target_text or "").strip().lower()
                if not t:
                    return False
                for snip in memory_snippets:
                    if len(snip) < 4:
                        continue
                    if t in snip or snip in t:
                        return True
                    if difflib.SequenceMatcher(None, t, snip).ratio() >= 0.60:
                        return True
                    match = difflib.SequenceMatcher(None, t, snip).find_longest_match(0, len(t), 0, len(snip))
                    if match.size >= max(4, int(len(t) * 0.5)):
                        return True
                return False

            if _is_polluted(intent_info.get("intent", "")):
                intent_info["intent"] = "質問" if any(q in current_user_text for q in ["？", "?", "何", "どう", "は"]) else "雑談"
            if _is_polluted(deliberation_info.get("breakdown_reason", "")):
                deliberation_info["breakdown_reason"] = ""
                deliberation_info["is_breakdown"] = False
            if _is_polluted(deliberation_info.get("consistency_correction", "")):
                deliberation_info["consistency_correction"] = ""
                deliberation_info["persona_risk"] = False

        if intent_info.get("reason") == "heuristic_third_party_hearsay":
            intent_info["target_is_ai"] = False
            deliberation_info["is_breakdown"] = False
            deliberation_info["breakdown_reason"] = ""
            deliberation_info["uncertainty_level"] = "low"

    except Exception as e:
        log.warning("pre reply analyzer failed; falling back to heuristic: %r", e)

    return intent_info, selected_lines, channel_summary, deliberation_info
