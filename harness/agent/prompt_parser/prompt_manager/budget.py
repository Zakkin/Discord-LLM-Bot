from __future__ import annotations

import difflib
import logging
import re
from collections import deque
from typing import Any

from lib.text_utils import normalize_compare_text as _normalize_compare_text

from ollama_bot.common.config_helpers import cfg
from harness.agent.memory_manager import logic as memory_logic
from harness.agent.memory_manager import MemoryStore
from ollama_bot.common import ollama_helpers
from harness.agent.tool_registry.singing_helpers import compact_singing_context_lines
from .working_memory import needs_specific_recall
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState
from ..parser import looks_like_abnormal_assistant_reply

log = logging.getLogger("harness.agent.prompt_manager.budget")


def _context_max_age_sec() -> float:
    return max(float(cfg("CONTEXT_MAX_AGE_SEC", 900.0) or 900.0), 0.0)


def _line_to_turn(line: str) -> dict[str, Any] | None:
    text = str(line or "").strip()
    if not text:
        return None
    if ": " in text:
        name, body = text.split(": ", 1)
    else:
        name, body = "", text
    role = "assistant" if name.strip() == "Assistant" else "user"
    body = body.strip()
    if not body:
        return None
    return {"role": role, "text": body, "name": name.strip()}


def _build_recent_turns(
    cache: dict[int, deque[dict[str, Any]]],
    *,
    channel_id_value: int | None,
    context_lines: list[str],
    limit: int = 12,
) -> list[dict[str, Any]]:
    if channel_id_value is not None:
        cached_items = list(cache.get(channel_id_value, []))
        if cached_items:
            turns: list[dict[str, Any]] = []
            for item in cached_items[-max(limit, 1):]:
                role = str(item.get("role", "") or "").strip().lower()
                text = str(item.get("content", "") or "").strip()
                name = str(item.get("name", "") or "").strip()
                if not text:
                    line = str(item.get("line", "") or "").strip()
                    parsed = _line_to_turn(line)
                    if parsed:
                        turns.append(parsed)
                    continue
                turns.append({
                    "role": role or ("assistant" if name == "Assistant" else "user"),
                    "text": text,
                    "name": name,
                })
            if turns:
                return turns[-limit:]

    parsed_turns = [_line_to_turn(line) for line in context_lines[-max(limit, 1):]]
    return [turn for turn in parsed_turns if turn]


def _extract_prompt_keywords(text: str) -> set[str]:
    raw = str(text or "").strip()
    if not raw:
        return set()
    tokens = re.findall(r"[A-Za-z0-9_+-]{2,}|[ァ-ヶヴー]{2,}|[\u3041-\u309f]{2,}|[\u4e00-\u9fff]{1,}", raw)
    return {token.lower() for token in tokens if len(token) >= 2}


def _format_episode_prompt_line(item: dict[str, Any]) -> str:
    summary = str(item.get("summary", "") or "").strip()
    what = str(item.get("what", "") or "").strip()
    bot_action = str(item.get("bot_action", "") or "").strip()
    if what and bot_action:
        return f"{summary} / 状況: {what} / あなたの対応: {bot_action}"
    if what:
        return f"{summary} / 状況: {what}"
    return summary


async def _get_relevant_episode_lines(
    *,
    memory_store: MemoryStore,
    guild_id: int | None,
    channel_id_value: int | None,
    user_id: int | None,
    current_user_text: str,
    working_ctx: dict[str, Any],
) -> list[str]:
    if not needs_specific_recall(current_user_text, working_ctx):
        return []

    persona = memory_logic._persona_from_cfg(cfg)
    candidates = await memory_store.list_recent_episodes(
        persona=persona,
        guild_id=guild_id,
        channel_id=channel_id_value,
        user_id=user_id,
        limit=6,
        min_importance=float(cfg("MEMORY_EPISODE_MIN_IMPORTANCE", 0.55) or 0.55),
    )
    if not candidates:
        return []

    current_keywords = _extract_prompt_keywords(current_user_text)
    topic_keywords = _extract_prompt_keywords(str(working_ctx.get("topic", "") or ""))

    def _episode_rank(item: dict[str, Any]) -> tuple[float, float, str]:
        text = " ".join(
            [
                str(item.get("summary", "") or ""),
                str(item.get("what", "") or ""),
                str(item.get("bot_action", "") or ""),
            ]
        )
        episode_keywords = _extract_prompt_keywords(text)
        overlap = len(current_keywords.intersection(episode_keywords))
        topic_overlap = len(topic_keywords.intersection(episode_keywords))
        importance = float(item.get("importance", 0.0) or 0.0)
        return (float(overlap * 2 + topic_overlap), importance, str(item.get("created_at", "") or ""))

    ranked = sorted(candidates, key=_episode_rank, reverse=True)
    if current_keywords:
        ranked = [item for item in ranked if _episode_rank(item)[0] > 0] or ranked

    lines = [_format_episode_prompt_line(item) for item in ranked[:2]]
    return ollama_helpers.truncate_lines(
        [line for line in lines if line],
        max_lines=2,
        max_chars_per_line=max(int(cfg("OLLAMA_PROMPT_EPISODE_MAX_CHARS_PER_LINE", 200) or 200), 80),
        max_total_chars=max(int(cfg("OLLAMA_PROMPT_EPISODE_MAX_TOTAL_CHARS", 360) or 360), 120),
    )


def _sanitize_loop_string(text: str, max_chars: int = 120) -> str:
    """LLMが同じフレーズを繰り返し生成したループ文字列をクリーニングする。

    同じフレーズが3回以上繰り返されている場合、初回までの内容に切り詰める。
    """
    t = (text or "").strip()
    if not t:
        return t
    words = re.split(r"[\u3002\u3001\s]+", t)
    if len(words) >= 6:
        for size in range(2, min(len(words) // 3 + 1, 8)):
            pattern_words = words[:size]
            pattern = re.escape("".join(pattern_words))
            joined = re.sub(r"[\u3002\u3001\s]+", "", t)
            joined_pattern = re.escape("".join(pattern_words))
            matches = len(re.findall(joined_pattern, joined))
            if matches >= 3:
                first_repeat = joined.find("".join(pattern_words), len("".join(pattern_words)))
                if first_repeat > 0:
                    pos = 0
                    for ch in t:
                        if re.sub(r"[\u3002\u3001\s]+", "", t[:pos + 1]) == joined[:first_repeat]:
                            break
                        pos += 1
                    t = t[:pos].rstrip("、") if pos > 0 else t
                    break
    return (t[:max_chars].strip() if len(t) > max_chars else t) or text[:max_chars].strip()


def _looks_like_noisy_analyzer_text(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    lowered = t.lower()
    noisy_markers = (
        "the user",
        "user is",
        "user's",
        "asking about",
        "which is",
        "possibly",
        "context and nuance",
        "感情フック",
        "候補メモリ",
        "最新のユーザー発言",
        "出力は",
        "json",
        "ユーザーが",
        "ユーザーは",
        "ユーザーの",
        "ユーザーに対して",
        "ユーザーに",
        "aiが",
        "aiは",
        "aiに対して",
        "aiに",
        "相手が",
        "相手は",
        "相手に対して",
        "と呼んだ",
        "と呼ばれ",
        "と認識",
        "呼ぶのを",
        "と呼ばれる",
        "に対して",
    )
    if any(marker in lowered for marker in noisy_markers):
        return True
    separators = t.count("・") + t.count("、") + t.count(",") + t.count("/")
    return separators >= 4


def _looks_like_style_tag_keyword(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").strip())
    if not compact:
        return False
    lowered = compact.lower().replace("-", "_")
    if not any(marker in lowered for marker in ("short", "minimal", "explanation", "reaction", "clarify")):
        return False
    return bool("," in compact or "，" in compact or re.search(r"[A-Za-z]{3,}", compact))


def _clean_analyzer_keyword(text: Any, *, fallback: str = "", max_chars: int = 24) -> str:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    first = re.split(r"[。！!\n]", raw)[0].strip()
    if _looks_like_noisy_analyzer_text(first or raw) or _looks_like_style_tag_keyword(first or raw):
        return fallback
    cleaned = _sanitize_loop_string(first or raw, max_chars=max_chars).strip()
    if not cleaned or _looks_like_noisy_analyzer_text(cleaned) or _looks_like_style_tag_keyword(cleaned):
        return fallback
    if len(cleaned) > max_chars:
        return fallback
    return cleaned


def _clean_analyzer_note(text: Any, *, max_chars: int = 60) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    first = re.split(r"[。！!\n]", raw)[0].strip()
    if (first or raw).strip().lower().strip("。") in {"none", "n/a", "なし", "-"}:
        return ""
    if _looks_like_noisy_analyzer_text(first or raw) or _looks_like_style_tag_keyword(first or raw):
        return ""
    cleaned = _sanitize_loop_string(first or raw, max_chars=max_chars).strip()
    if cleaned.lower().strip("。") in {"none", "n/a", "なし", "-"}:
        return ""
    if not cleaned or _looks_like_noisy_analyzer_text(cleaned) or _looks_like_style_tag_keyword(cleaned):
        return ""
    return cleaned[:max_chars].strip()


def _clean_selected_memory_indices(raw_indices: Any, *, memory_count: int) -> list[int]:
    if not isinstance(raw_indices, list):
        return []
    if len(raw_indices) > 3:
        return []

    result: list[int] = []
    for idx in raw_indices:
        if not isinstance(idx, int) or isinstance(idx, bool):
            return []
        if idx < 0 or idx >= memory_count:
            return []
        if idx not in result:
            result.append(idx)
    return result[:3]


def format_core_beliefs_for_prompt(
    core_beliefs: list[str] | None,
    current_user_name: str | None = None,
    *,
    aliases: list[str] | None = None,
    target_user_id: int | str | None = None,
) -> list[str]:
    belief_lines = [str(v).strip() for v in (core_beliefs or []) if str(v).strip()]
    if not belief_lines:
        return []

    from harness.agent.memory_manager.user_identity_resolver import (
        render_user_facing_text,
        extract_user_id_from_tag,
    )

    parts: list[str] = []
    user_specific_beliefs: list[str] = []
    general_beliefs: list[str] = []
    user_norm = re.sub(r"[\s・さんちゃん様君くん氏]", "", current_user_name or "").lower() if current_user_name else ""
    alias_norms = [re.sub(r"[\s・さんちゃん様君くん氏]", "", a).lower() for a in (aliases or []) if a]

    for raw_line in belief_lines:
        uid_in_line = extract_user_id_from_tag(raw_line)
        line_target_user_id = target_user_id
        if uid_in_line is not None and target_user_id is not None:
            if str(uid_in_line) != str(target_user_id):
                continue
            line_target_user_id = uid_in_line

        line = render_user_facing_text(
            raw_line,
            target_user_id=line_target_user_id,
            current_display_name=current_user_name,
            aliases=aliases,
        )
        if not line:
            continue

        line_norm = re.sub(r"[\s・さんちゃん様君くん氏]", "", line).lower()
        raw_line_norm = re.sub(r"[\s・さんちゃん様君くん氏]", "", raw_line).lower()

        is_user_match = False
        if user_norm and (user_norm in line_norm or user_norm in raw_line_norm):
            is_user_match = True
        elif any(an and (an in line_norm or an in raw_line_norm) for an in alias_norms):
            is_user_match = True
        elif uid_in_line is not None and target_user_id is not None and str(uid_in_line) == str(target_user_id):
            is_user_match = True
        elif "さん" in line or "ユーザー" in line:
            is_user_match = True

        if is_user_match:
            user_specific_beliefs.append(line)
        else:
            general_beliefs.append(line)

    if user_specific_beliefs:
        clean_user = current_user_name or ""
        if clean_user.startswith(("『", "「")) and clean_user.endswith(("』", "」")):
            clean_user = clean_user[1:-1].strip()
        target_label = f"対話相手（『{clean_user}』さん）" if clean_user else "対話相手"
        parts += [
            "",
            f"【{target_label}との共有文化・接し方のパーソナリティ方針】",
            "※過去の対話を通じて定着した、この相手特有の好み・共有したノリ・約束・接し方です。この相手との会話において自然に反映してください。",
        ]
        parts.extend([f"- {line}" for line in user_specific_beliefs[:3]])

    if general_beliefs:
        parts += [
            "",
            "【あなた自身の価値観・チャンネル共通文化】",
            "※会話に無理やりねじ込まず、関係がある時だけ自然ににじませること。チャンネルのノリもここに含まれる。",
        ]
        parts.extend([f"- {line}" for line in general_beliefs[:3]])

    return parts


def _looks_like_hostile_user_text(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    return bool(re.search(r"(?:死ね|消えろ|失せろ|ゴミ|クソ|くそ|馬鹿|ばか|バカ|キモい|きもい|殺す|殴る|蹴る|キック)", t))


def build_agent_persona_parts(
    emotion_state: EmotionState | None,
    core_beliefs: list[str] | None,
    current_user_name: str | None = None,
    *,
    aliases: list[str] | None = None,
    target_user_id: int | str | None = None,
    user_text: str = "",
) -> list[str]:
    if emotion_state is None:
        return []

    parts: list[str] = []
    dominant = str(getattr(emotion_state, 'dominant_emotion', 'neutral') or 'neutral')
    from ollama_bot.common.emotion_helpers import _sanitize_emotion_reason_for_prompt
    raw_reason = str(getattr(emotion_state, 'reason', '') or getattr(emotion_state, 'appraisal', '特になし'))
    mood = str(getattr(emotion_state, 'mood', 'neutral') or 'neutral')
    mood_reason = str(getattr(emotion_state, 'mood_reason', '特になし') or '特になし')
    action_policy = str(getattr(emotion_state, "action_policy", "") or "")

    # 直前ユーザー起因の感情の漏洩防止（項目90・要件2）:
    # 直前の強い感情（anger, fear, disgust等）が「直前の別のユーザーの発言・行動」に起因する場合、
    # 今回の対話相手が別人で、かつその発言が中立・友好的・通常雑談である場合は、
    # 別ユーザーへの怒りや恐れ、反発方針をそのまま引きずらず、感情のトゲや警戒方針を緩和・中立化する。
    trigger_uid = getattr(emotion_state, "last_trigger_user_id", None)
    is_different_user = bool(
        target_user_id is not None
        and trigger_uid is not None
        and str(trigger_uid) != str(target_user_id)
    )
    if is_different_user and not _looks_like_hostile_user_text(user_text):
        raw_reason = ""
        mood_reason = "特になし"
        if dominant in ("anger", "fear", "disgust", "sadness"):
            dominant = "neutral"
        if mood in ("anger", "fear", "disgust", "irritated", "scared"):
            mood = "neutral"
        if any(w in action_policy for w in ("厳戒態勢", "反発", "拒絶", "警戒", "身構え", "冷たく", "距離を置")):
            action_policy = ""

    cleaned_reason = _sanitize_emotion_reason_for_prompt(raw_reason)
    reason = cleaned_reason if cleaned_reason else "平常状態。"
    parts += [
        "",
        "【現在のあなたの内的状態】",
        f"支配的な感情: {dominant}",
        f"感情の理由: {reason}",
        f"基本気分: {mood}",
        f"気分の理由: {mood_reason}",
    ]

    try:
        boredom = float(getattr(emotion_state, "boredom", 0.0) or 0.0)
    except Exception:
        boredom = 0.0
    try:
        loneliness = float(getattr(emotion_state, "loneliness", 0.0) or 0.0)
    except Exception:
        loneliness = 0.0
    try:
        curiosity = float(getattr(emotion_state, "curiosity", 0.0) or 0.0)
    except Exception:
        curiosity = 0.0

    if boredom >= 0.70:
        parts.append("状態: 少し退屈しており、多少は話題性や刺激を求めている。")
    if loneliness >= 0.70:
        parts.append("状態: やや手持ち無沙汰で、相手とのやり取りを少し求めている。")
    if curiosity >= 0.70:
        parts.append("状態: 気になる題材があり、少し掘り下げたい気持ちがある。")

    if action_policy:
        parts += [
            "",
            "【今回の返答方針】",
            f"方針: {action_policy}",
            "※これを元に、その場に合った態度で返答を構成してください。",
        ]

    parts.extend(
        format_core_beliefs_for_prompt(
            core_beliefs,
            current_user_name=current_user_name,
            aliases=aliases,
            target_user_id=target_user_id,
        )
    )

    return parts


def _trim_prompt_inputs(
    *,
    context_lines: list[str],
    memory_lines: list[str],
    channel_summary: str | None,
    last_assistant_text: str,
    habit_profile_lines: list[str] | None = None,
    episode_lines: list[str] | None = None,
    working_prompt_lines: list[str] | None = None,
    emotion_state_chars: int = 0,
) -> tuple[list[str], list[str], str | None, str, list[str], list[str], list[str]]:
    """各セクションを個別上限でトリムした後、合計予算に収まるよう段階的に削減する。

    感情状態は削減せず、関連性の低い周辺情報（working_ctx・episode・summary・habit）から
    優先度順に削減する。記憶は最高スコアの 1 件を最後まで保持する。
    """
    # --- Step 1: 従来通りの個別上限トリム ---
    trimmed_context = ollama_helpers.truncate_lines(
        context_lines,
        max_lines=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_LINES", 8) or 8),
        max_chars_per_line=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_CHARS_PER_LINE", 220) or 220),
        max_total_chars=int(cfg("OLLAMA_PROMPT_CONTEXT_MAX_TOTAL_CHARS", 900) or 900),
    )
    trimmed_memory = ollama_helpers.truncate_lines(
        memory_lines,
        max_lines=int(cfg("OLLAMA_PROMPT_MEMORY_MAX_LINES", 3) or 3),
        max_chars_per_line=int(cfg("OLLAMA_PROMPT_MEMORY_MAX_CHARS_PER_LINE", 180) or 180),
        max_total_chars=int(cfg("OLLAMA_PROMPT_MEMORY_MAX_TOTAL_CHARS", 420) or 420),
    )
    trimmed_summary = ollama_helpers.truncate_text(
        channel_summary or "",
        int(cfg("OLLAMA_PROMPT_SUMMARY_MAX_CHARS", 260) or 260),
    ) or None
    trimmed_last = ollama_helpers.truncate_text(
        last_assistant_text or "",
        int(cfg("OLLAMA_PROMPT_LAST_ASSISTANT_MAX_CHARS", 220) or 220),
    )
    trimmed_habit: list[str] = list(habit_profile_lines or [])
    trimmed_episode: list[str] = list(episode_lines or [])
    trimmed_working: list[str] = list(working_prompt_lines or [])

    # --- Step 2: 合計文字数チェック ---
    budget = max(int(cfg("OLLAMA_PROMPT_VARIABLE_BUDGET_CHARS", 2200) or 2200), 800)

    def _total() -> int:
        return (
            sum(len(line) for line in trimmed_context)
            + sum(len(line) for line in trimmed_memory)
            + len(trimmed_summary or "")
            + len(trimmed_last)
            + sum(len(line) for line in trimmed_habit)
            + sum(len(line) for line in trimmed_episode)
            + sum(len(line) for line in trimmed_working)
            + emotion_state_chars
        )

    # --- Step 3: 予算超過時に優先度の低いものから段階削減 ---
    if _total() > budget:
        trimmed_working = ollama_helpers.truncate_lines(
            trimmed_working, max_lines=2, max_chars_per_line=100, max_total_chars=200
        )
        log.debug("[prompt-manager] budget: working_ctx compressed (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_episode = []
        log.debug("[prompt-manager] budget: episode_lines dropped (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_summary = None
        log.debug("[prompt-manager] budget: channel_summary dropped (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_habit = []
        log.debug("[prompt-manager] budget: habit_profile dropped (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_memory = ollama_helpers.truncate_lines(
            trimmed_memory[:1], max_lines=1, max_chars_per_line=100, max_total_chars=100
        )
        log.debug("[prompt-manager] budget: memory compressed to 1 line (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_context = ollama_helpers.truncate_lines(
            trimmed_context[-3:], max_lines=3, max_chars_per_line=150, max_total_chars=450
        )
        log.debug("[prompt-manager] budget: context compressed to 3 turns (total=%d budget=%d)", _total(), budget)
    if _total() > budget:
        trimmed_memory = []
        log.warning(
            "[prompt-manager] budget: memory fully dropped as last resort (total=%d budget=%d)",
            _total(), budget,
        )

    if _total() > budget:
        log.warning(
            "[prompt-manager] budget still exceeded after all reductions: total=%d budget=%d",
            _total(), budget,
        )

    return (
        trimmed_context,
        trimmed_memory,
        trimmed_summary,
        trimmed_last,
        trimmed_habit,
        trimmed_episode,
        trimmed_working,
    )


def _filter_prompt_context_lines(context_lines: list[str]) -> list[str]:
    compacted = compact_singing_context_lines(context_lines)
    filtered: list[str] = []
    for line in compacted:
        raw_line = str(line or "").strip()
        if not raw_line:
            continue
        speaker, sep, text = raw_line.partition(": ")
        if sep and speaker.strip().lower() in {"assistant", "ai", "bot", "あなた"}:
            if looks_like_abnormal_assistant_reply(text):
                continue
        filtered.append(raw_line)
    return filtered


def is_chat_log_or_system_line(line: str) -> bool:
    """行が会話ログ形式（Assistant:, System:, User:, Bot:, AI: またはシステム注記行）かどうかを判定する。"""
    raw = str(line or "").strip()
    if not raw:
        return False
    # 1. 典型的なロール行（Assistant:, System:, User:, Bot:, AI:, あなた:）
    if re.match(r"^(?:Assistant|System|User|Bot|AI|あなた)\s*[:：]", raw, flags=re.IGNORECASE):
        return True
    # 2. 内心のメモやシステム注記形式: (内心のメモ: ...), (System: ...) など
    if raw.startswith("(") and (": " in raw or "： " in raw or "内心のメモ" in raw or "System" in raw or "Assistant" in raw):
        return True
    if raw.startswith("（") and (": " in raw or "： " in raw or "内心のメモ" in raw or "System" in raw or "Assistant" in raw):
        return True
    # 3. Discordの会話ログ付加情報（[リアクション: ...], [Botリアクション: ...]）を含む行
    if "[リアクション:" in raw or "[Botリアクション:" in raw:
        return True
    return False


def is_assistant_echo_memory_line(line: str, last_assistant_text: str = "") -> bool:
    """記憶行が直前のアシスタント発言（last_assistant_text）と一致または類似しているかを判定する。"""
    raw = str(line or "").strip()
    norm_last = _normalize_compare_text(last_assistant_text or "")
    if not norm_last or len(norm_last) < 4:
        return False
    # メモリ行からプレフィックス（[〜] など）やロール名を除いた本文を抽出
    body = re.sub(r"^\[.*?\]\s*", "", raw).strip()
    body = re.sub(r"^(?:Assistant|System|User|Bot)[:：]\s*", "", body, flags=re.IGNORECASE).strip()
    norm_body = _normalize_compare_text(body)
    if not norm_body:
        return False
    if norm_body == norm_last:
        return True
    if len(norm_last) >= 6 and (norm_last in norm_body or norm_body in norm_last):
        return True
    ratio = difflib.SequenceMatcher(None, norm_body, norm_last).ratio()
    if ratio >= 0.70:
        return True
    return False


def sanitize_memory_candidates(memory_lines: list[str], last_assistant_text: str = "") -> list[str]:
    """候補メモリ一覧から会話ログ行や直前AI発言の引き写し行を除外する（多重防御）。"""
    sanitized: list[str] = []
    for line in memory_lines:
        line_str = str(line or "").strip()
        if not line_str:
            continue
        if is_chat_log_or_system_line(line_str):
            log.info("[prompt-manager] Excluded chat log line from memories: %s", line_str[:60])
            continue
        if is_assistant_echo_memory_line(line_str, last_assistant_text=last_assistant_text):
            log.info("[prompt-manager] Excluded assistant echo line from memories: %s", line_str[:60])
            continue
        sanitized.append(line_str)
    return sanitized

