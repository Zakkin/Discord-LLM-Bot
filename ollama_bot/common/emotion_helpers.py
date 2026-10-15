from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from typing import Any

from lib.json_utils import save_json_atomic
from .config_helpers import cfg_bool, cfg_float, cfg_int, cfg_str

log = logging.getLogger(__name__)


EMOTION_KEYS = ("joy", "anticipation", "anger", "disgust", "sadness", "surprise", "fear")

EMOTION_LABEL_TO_FACE = {
    "joy": "🤩",
    "anticipation": "😋",
    "anger": "😡",
    "disgust": "😒",
    "sadness": "😢",
    "surprise": "😳",
    "fear": "😨",
    "neutral": "😐",
}

EMOTION_LABEL_TO_JA = {
    "joy": "喜び",
    "anticipation": "期待",
    "anger": "怒り",
    "disgust": "嫌悪",
    "sadness": "悲しみ",
    "surprise": "驚き",
    "fear": "恐れ",
    "neutral": "通常",
}


def _split_context_line(line: str) -> tuple[str, str]:
    raw = str(line or "").strip()
    if not raw:
        return "", ""
    if ":" not in raw:
        return "", raw
    speaker, text = raw.split(":", 1)
    return speaker.strip(), text.strip()


def _is_internal_or_system_context_line(line: str, speaker: str, text: str) -> bool:
    raw_line = str(line or "").strip()
    spk = str(speaker or "").strip()
    spk_lower = spk.lower()
    txt = str(text or "").strip()

    if spk_lower == "system":
        return True

    memo_markers = ("内心のメモ", "(内心のメモ", "（内心のメモ")
    if any(m in spk for m in memo_markers) or any(m in txt for m in memo_markers) or any(m in raw_line for m in memo_markers):
        return True

    if (raw_line.startswith(("（", "(")) or spk.startswith(("（", "("))) and "の記憶・実感" in raw_line:
        return True

    reply_ref_prefixes = ("（あなたの「", "(あなたの「", "（あなたの", "(あなたの")
    if any(raw_line.startswith(p) for p in reply_ref_prefixes) or any(spk.startswith(p) for p in reply_ref_prefixes):
        return True

    return False


def relabel_emotion_context_line(
    line: str,
    *,
    self_label: str = "あなた",
    self_aliases: set[str] | None = None,
) -> str:
    raw_line = str(line or "").strip()
    if not raw_line:
        return ""
    speaker, text = _split_context_line(line)
    if not text:
        return ""

    if _is_internal_or_system_context_line(raw_line, speaker, text):
        return ""

    aliases = {alias.strip().lower() for alias in (self_aliases or set()) if str(alias or "").strip()}
    speaker_key = speaker.lower()
    speaker_key_without_face = speaker_key.lstrip("".join(EMOTION_LABEL_TO_FACE.values()) + " ")
    if speaker_key == "assistant" or (speaker_key and (speaker_key in aliases or speaker_key_without_face in aliases)):
        return f"{self_label}: {text}"
    if speaker:
        return f"相手({speaker}): {text}"
    return f"相手: {text}"


def build_emotion_evaluation_sections(
    context_lines: list[str],
    latest_user_text: str,
    *,
    self_label: str = "あなた",
    self_aliases: set[str] | None = None,
    is_reply_to_other_user: bool = False,
    reply_target_user: str = "",
) -> list[str]:
    parts: list[str] = []
    relabeled_context = [
        relabeled
        for line in (context_lines or [])
        if (relabeled := relabel_emotion_context_line(line, self_label=self_label, self_aliases=self_aliases))
    ]
    if relabeled_context:
        parts.append(f"会話の背景（{self_label}が自分、それ以外は相手）:")
        parts.extend(relabeled_context)
        parts.append("")

    raw_latest = (latest_user_text or "").strip()
    target_user = (reply_target_user or "").strip()
    is_other_reply = is_reply_to_other_user

    aliases = {alias.strip().lower() for alias in (self_aliases or set()) if str(alias or "").strip()}
    aliases.add(self_label.strip().lower())
    aliases.add("あなた")
    aliases.add("assistant")

    # （〇〇の「...」への返信） または （〇〇への返信） の検出
    reply_match = re.search(r"[（(]([^\s（）()「」\n]{1,40}?)(?:の「.+?」)?への返信[）)]", raw_latest, flags=re.DOTALL)
    if reply_match:
        matched_target = reply_match.group(1).strip()
        matched_target_clean = re.sub(r"^(?:相手|ユーザー)\((.*?)\)$", r"\1", matched_target).strip()
        if matched_target_clean.lower() not in aliases and matched_target.lower() not in aliases:
            is_other_reply = True
            if not target_user:
                target_user = matched_target_clean or matched_target

    parts.append("最新の相手の発言:")
    if is_other_reply:
        target_display = f"『{target_user}』さん" if target_user else "別ユーザー"
        parts.append(
            f"※最新の発言は、別ユーザー{target_display}の発言に対する返信（人間同士の会話）です。"
            f"返信先の発言はあなた（{self_label}）のものではありません。"
            "ロールを反転させて『自分の愚痴や発言が否定された』と誤認しないでください。"
        )
    parts.append(raw_latest or "（本文なし）")
    return parts


def emotion_scoring_enabled() -> bool:
    return cfg_bool("EMOTION_SCORING_ENABLED", True)


def emotion_bio_enabled() -> bool:
    from .config_helpers import cfg_emotion_bio_enabled
    return cfg_emotion_bio_enabled()


def clamp_score(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.0


def normalize_llm_emotion_scores(scores: dict[str, Any] | None) -> dict[str, float]:
    source = scores or {}
    numeric: dict[str, float] = {}
    for key in EMOTION_KEYS:
        try:
            numeric[key] = float(source.get(key, 0.0))
        except Exception:
            numeric[key] = 0.0

    values = [max(0.0, value) for value in numeric.values()]
    max_value = max(values, default=0.0)
    over_one_count = sum(1 for value in values if value > 1.0)
    looks_like_percentage_scale = max_value > 5.0 or over_one_count >= 2

    normalized = {key: max(0.0, value) for key, value in numeric.items()}
    if looks_like_percentage_scale and max_value <= 100.0:
        normalized = {key: (value / 100.0) for key, value in normalized.items()}

    return {key: clamp_score(value) for key, value in normalized.items()}


def merge_emotion_scores(*score_maps: dict[str, float] | None) -> dict[str, float]:
    merged = {key: 0.0 for key in EMOTION_KEYS}
    for scores in score_maps:
        source = scores or {}
        for key in EMOTION_KEYS:
            merged[key] = max(merged[key], clamp_score(source.get(key, 0.0)))
    return merged


def apply_emotion_persona_weights(scores: dict[str, float] | None) -> dict[str, float]:
    source = scores or {}
    weights = {
        "joy": cfg_float("EMOTION_PERSONA_WEIGHT_JOY", 1.0),
        "anticipation": cfg_float("EMOTION_PERSONA_WEIGHT_ANTICIPATION", 1.0),
        "anger": cfg_float("EMOTION_PERSONA_WEIGHT_ANGER", 0.85),
        "disgust": cfg_float("EMOTION_PERSONA_WEIGHT_DISGUST", 1.0),
        "sadness": cfg_float("EMOTION_PERSONA_WEIGHT_SADNESS", 1.0),
        "surprise": cfg_float("EMOTION_PERSONA_WEIGHT_SURPRISE", 1.0),
        "fear": cfg_float("EMOTION_PERSONA_WEIGHT_FEAR", 1.0),
    }
    adjusted = {key: 0.0 for key in EMOTION_KEYS}
    for key in EMOTION_KEYS:
        adjusted[key] = clamp_score(source.get(key, 0.0) * weights.get(key, 1.0))
    return adjusted


def should_force_deliberation_from_emotions(
    scores: dict[str, float] | None,
    *,
    curiosity: float = 0.0,
    anger_threshold: float = 0.65,
    surprise_threshold: float = 0.60,
    curiosity_threshold: float = 0.68,
) -> tuple[bool, str]:
    anger_score = clamp_score((scores or {}).get("anger", 0.0))
    if anger_score >= clamp_score(anger_threshold):
        return True, "anger"

    surprise_score = clamp_score((scores or {}).get("surprise", 0.0))
    if surprise_score >= clamp_score(surprise_threshold):
        return True, "surprise"

    curiosity_score = clamp_score(curiosity)
    if curiosity_score >= clamp_score(curiosity_threshold):
        return True, "curiosity"

    return False, ""


def should_force_thinking_from_emotions(
    scores: dict[str, float] | None,
    *,
    curiosity: float = 0.0,
    anger_threshold: float = 0.65,
    surprise_threshold: float = 0.60,
    curiosity_threshold: float = 0.68,
) -> tuple[bool, str]:
    return should_force_deliberation_from_emotions(
        scores,
        curiosity=curiosity,
        anger_threshold=anger_threshold,
        surprise_threshold=surprise_threshold,
        curiosity_threshold=curiosity_threshold,
    )


_SIMPLIFIED_CHINESE_CHARS = set(
    "这那个们了在是和与及被把让对将并且嗎吗，；：【】《》"
    "继续说话没很还着觉车门头发边东买卖听给"
)
_CHINESE_PHRASE_PATTERN = re.compile(
    r"(会继续|继续|觉得|由于|因为|所以|虽然|但是|没有|什么|怎么|怎样|这样|那样)"
)


def contains_cjk_non_japanese(text: str) -> bool:
    if not text:
        return False
    if _CHINESE_PHRASE_PATTERN.search(text):
        return True
    for ch in text:
        code = ord(ch)
        if 0x3040 <= code <= 0x30FF:
            continue
        if ch in _SIMPLIFIED_CHINESE_CHARS:
            return True
    return False


def looks_like_bad_reason_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if contains_cjk_non_japanese(t):
        return True
    has_predicate = bool(re.search(r"(した|している|していた|なった|感じた|覚えた|見えた|受けた|ため|ので|から|です|ます|だ。|だった)", t))
    if not has_predicate:
        return True
    from lib.config_utils import cfg
    bot_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    patterns = [
        r"AI.*?うんこ.*?してください",
        r"(?:私|自分|AI|こちら)の愚痴を(?:無視|スルー)",
        r"愚痴を(?:ぶっつけ|言っ|吐い)たのに",
    ]
    if bot_name:
        patterns.append(re.escape(bot_name))
    for ptn in patterns:
        if re.search(ptn, t):
            return True
    return False


def normalize_reason_text(text: str, max_chars: int) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    t = t.replace('"', "").replace("'", "")
    t = t.strip(" 、。")
    from lib.config_utils import cfg
    bot_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if bot_name:
        t = t.replace(bot_name, "AI")
    t = t.replace("ユーザー", "相手")
    if t and not t.endswith("。"):
        t += "。"
    from . import ollama_helpers
    t = ollama_helpers.truncate_text(t, max_chars)
    return t or "相手の発言に不満を覚えたためです。"


def fallback_reason_text(emotion_key: str) -> str:
    fallback_map = {
        "joy": "相手の発言を前向きに受け取ったためです。",
        "anticipation": "相手の発言の続きや反応を期待したためです。",
        "anger": "相手の発言に苛立ちを覚えたためです。",
        "disgust": "相手の発言に嫌悪感を覚えたためです。",
        "sadness": "相手の発言を受けて気落ちしたためです。",
        "surprise": "相手の発言が予想外だったためです。",
        "fear": "相手の発言に不安を覚えたためです。",
        "neutral": "平常状態です。",
    }
    return fallback_map.get(emotion_key, "平常状態です。")


def adapt_emotion_name(emotion_key: str, reason: str) -> str:
    emotion_name = EMOTION_LABEL_TO_JA.get(emotion_key, "通常")
    if emotion_key == "joy" and reason:
        if any(w in reason for w in ("安心", "ホッと", "落ち着", "共感", "同情", "限界")):
            emotion_name = "安心・和み"
    return emotion_name

def build_emotion_bio_text(emotion_key: str, reason: str) -> str:
    body = normalize_reason_text((reason or "").strip(), cfg_int("EMOTION_BIO_MAX_CHARS", 180)) or "平常状態です。"
    emotion_name = adapt_emotion_name(emotion_key, body)
    return f"現在の感情: {emotion_name}\n理由: {body}"


def format_single_config_bio_section(label: str, emotion_key: str, reason: str) -> str:
    base = build_emotion_bio_text(emotion_key, reason)
    clean_label = (label or "").strip()
    if clean_label:
        return f"【{clean_label}】\n{base}"
    return base


def build_multi_config_bio_text(entries: dict[str, dict[str, Any]]) -> str:
    """複数コンフィグのエントリ辞書から結合されたプロフィール文字列を生成する。"""
    if not entries:
        return ""
    if len(entries) == 1:
        entry = next(iter(entries.values()))
        raw_label = str(entry.get("label", "") or "").strip()
        emotion_key = str(entry.get("emotion_key", "") or "neutral")
        reason = str(entry.get("reason", "") or "")
        return format_single_config_bio_section(raw_label, emotion_key, reason)

    # 複数エントリがある場合はソート（img を最優先、他を辞書順）して結合
    sections: list[str] = []
    sorted_keys = sorted(
        entries.keys(),
        key=lambda k: (0 if "img" in k.lower() else 1, k)
    )
    for k in sorted_keys:
        e = entries[k]
        label = str(e.get("label", "") or "").strip() or k
        emotion_key = str(e.get("emotion_key", "") or "neutral")
        reason = str(e.get("reason", "") or "")
        sections.append(format_single_config_bio_section(label, emotion_key, reason))

    return "\n\n".join(sections)


def update_shared_emotion_bio(
    config_name: str,
    label: str,
    emotion_key: str,
    reason: str,
    *,
    enabled: bool = True,
    shared_path: str | None = None,
) -> str:
    """共有Bio状態ファイルをアトミックに更新し、マージされた全体の自己紹介テキストを返す。"""
    if shared_path is None:
        raw_path = cfg_str("EMOTION_BIO_SHARED_PATH", "emotion_bio_shared.json")
        shared_path = os.path.abspath(raw_path)

    lock_path = f"{shared_path}.lock"
    ttl_sec = cfg_float("EMOTION_BIO_ENTRY_TTL_SEC", 604800.0)
    now = time.time()

    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass

            entries: dict[str, dict[str, Any]] = {}
            if os.path.exists(shared_path):
                try:
                    with open(shared_path, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                        if isinstance(data, dict):
                            entries = {k: v for k, v in data.items() if isinstance(v, dict)}
                except Exception:
                    entries = {}

            # TTL切れの古いエントリをパージ
            valid_entries: dict[str, dict[str, Any]] = {}
            for k, v in entries.items():
                updated_at = float(v.get("updated_at", 0.0) or 0.0)
                if (now - updated_at) < ttl_sec:
                    valid_entries[k] = v

            if enabled:
                valid_entries[config_name] = {
                    "label": label,
                    "emotion_key": emotion_key,
                    "reason": reason,
                    "updated_at": now,
                }
            else:
                valid_entries.pop(config_name, None)

            # アトミック保存
            save_json_atomic(shared_path, valid_entries)
            return build_multi_config_bio_text(valid_entries)
        finally:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(lock_fd)
    except Exception as e:
        log.warning("shared emotion bio update error: %r", e)
        return build_emotion_bio_text(emotion_key, reason)


def build_emotion_status_text(emotion_key: str, reason: str) -> str:
    return adapt_emotion_name(emotion_key, reason)


EMOTION_NOUN_MAP: dict[str, str] = {
    "joy": "明るさや喜び",
    "anticipation": "期待や関心",
    "anger": "苛立ちや刺々しさ",
    "disgust": "嫌悪感や引き気味な気持ち",
    "sadness": "沈み気味な気落ち",
    "surprise": "驚きや戸惑い",
    "fear": "警戒や不安",
}

EMOTION_TONE_NOUN_MAP: dict[str, str] = {
    "joy": "明るさ",
    "anticipation": "期待感",
    "anger": "苛立ち",
    "disgust": "引き気味な態度",
    "sadness": "沈んだ調子",
    "surprise": "戸惑い",
    "fear": "警戒心",
}

EMOTION_SINGLE_TONE_MAP: dict[str, str] = {
    "joy": "明るく前向きな調子",
    "anticipation": "期待や関心を込めた調子",
    "anger": "苛立ちや刺々しさがにじむ調子",
    "disgust": "引き気味で距離を置く調子",
    "sadness": "沈み気味で控えめな調子",
    "surprise": "驚きや戸惑いがにじむ調子",
    "fear": "警戒や不安がにじむ慎重な調子",
}


def _emotion_strength_adj(score: float) -> str:
    s = clamp_score(score)
    if s >= 0.82:
        return "非常に強い"
    if s >= 0.65:
        return "かなり強い"
    if s >= 0.48:
        return "はっきりとした"
    if s >= 0.30:
        return "少しの"
    return "かすかな"


def _emotion_strength_word(score: float) -> str:
    s = clamp_score(score)
    if s >= 0.82:
        return "非常に強く"
    if s >= 0.65:
        return "かなり強く"
    if s >= 0.48:
        return "はっきりと"
    if s >= 0.30:
        return "少し"
    return "ごく薄く"


def _emotion_tone_fragment(emotion_key: str, score: float) -> str:
    strength = _emotion_strength_word(score)
    fragment_map = {
        "joy": f"{strength}明るさがにじむ",
        "anticipation": f"{strength}期待や関心がにじむ",
        "anger": f"{strength}刺々しさがにじむ",
        "disgust": f"{strength}引き気味な空気がにじむ",
        "sadness": f"{strength}沈み気味な空気がにじむ",
        "surprise": f"{strength}戸惑いがにじむ",
        "fear": f"{strength}警戒がにじむ",
    }
    return fragment_map.get(emotion_key, f"{strength}揺れのある")


def _summarize_emotion_mix(scores: dict[str, float]) -> tuple[str, list[tuple[str, float]]]:
    filtered: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        value = clamp_score((scores or {}).get(key, 0.0))
        if value >= 0.18:
            filtered.append((key, value))
    ranked = sorted(filtered, key=lambda x: x[1], reverse=True)
    if not ranked:
        return "大きな感情の偏りはなく、比較的落ち着いた状態です。", []
    top_key, top_score = ranked[0]
    top_noun = EMOTION_NOUN_MAP.get(top_key, "感情の揺れ")
    top_adj = _emotion_strength_adj(top_score)

    if len(ranked) == 1:
        return f"{top_adj}{top_noun}が前面に出ています。", ranked
    second_key, second_score = ranked[1]
    if second_score >= top_score * 0.72:
        second_noun = EMOTION_NOUN_MAP.get(second_key, "感情の揺れ")
        second_adj = _emotion_strength_adj(second_score)
        return (
            f"{top_adj}{top_noun}を軸にしつつ、"
            f"{second_adj}{second_noun}も混ざっています。",
            ranked,
        )
    return f"{top_adj}{top_noun}がやや強めに出ています。", ranked


def build_nickname_with_face(base_name: str, emotion_key: str) -> str:
    return f"{EMOTION_LABEL_TO_FACE.get(emotion_key, '😐')} {base_name}".strip()


def _sanitize_emotion_reason_for_prompt(reason: str) -> str:
    """プロンプトに注入する感情理由テキストを客観的・中立的にサニタイズする。"""
    from . import ollama_helpers
    t = (reason or "").strip()
    if not t or t in ("平常状態", "特になし", "うまく言葉にできない状態です。"):
        return ""

    # 絵文字・顔文字・カギ括弧の除去
    t = re.sub(r"[\U00010000-\U0010ffff]", "", t)
    t = re.sub(r"[\(（][^()（）]{1,20}[\)）]", "", t)
    t = re.sub(r"[「」『』\"']", "", t)

    # 「コイトとしては」「AIとしては」等の主語を除去
    t = re.sub(r"(?:^|[、,\s])[^、。\n]{1,20}(?:としては|にとって|からすると)[、,\s]*", "、", t)

    # 「ぴゃっ…！」「わっ…！」「ひっ…！」などのセリフ感嘆詞を除去
    t = re.sub(r"[ぴひわキャギャえうお][ゃゅょぁぃぅぇぉっー〜!！…\.\?？\s]+", "", t)

    # 被害妄想・命令言及の客観化（「突然命令されて」「命令されたら怖い」等）
    t = re.sub(r"(?:突然|いきなり)?(?:命令|指示|要求)されて[、,\s]*", "予期せぬ強い調子を受けて", t)
    t = re.sub(r"(?:命令|指示)されたら[^。]+", "少し身構えている状態。", t)

    # セリフ調末尾（〜ですよ…！、〜なんですよ…！、〜ですから…等）を客観描写に丸める
    t = re.sub(r"(?:なんですよ|ですよ|ですね|ますね|ましたね|んです|わけですよ|からです|からでした)[!！…\.\?？~〜\s]*$", "状態。", t)

    # 記号の重複や文頭・文末の整形
    t = re.sub(r"[、,]{2,}", "、", t)
    t = re.sub(r"^[、,\s]+", "", t)
    t = re.sub(r"[、,\s]+$", "。", t)

    return ollama_helpers.truncate_text(t.strip(), 100) or ""


def build_reply_emotion_guidance(scores: dict[str, float], reason: str = "") -> str:
    """感情スコア辞書と理由テキストから、LLMへの返答指針テキストを生成する。"""
    emotion_summary, ranked = _summarize_emotion_mix(scores)

    if not ranked:
        tone = "通常どおり自然な調子。"
    elif len(ranked) >= 2 and ranked[1][1] >= ranked[0][1] * 0.72:
        top_key = ranked[0][0]
        second_key = ranked[1][0]
        top_tone_noun = EMOTION_TONE_NOUN_MAP.get(top_key, "感情")
        second_tone_noun = EMOTION_TONE_NOUN_MAP.get(second_key, "感情")
        tone = f"{top_tone_noun}と{second_tone_noun}が合わさった温度感を自然に反映。"
    else:
        top_key, top_score = ranked[0]
        tone_phrase = EMOTION_SINGLE_TONE_MAP.get(top_key, "自然な調子")
        if top_score >= 0.65:
            tone = f"強めの{tone_phrase}を自然に反映。"
        elif top_score <= 0.30:
            tone = f"かすかに{tone_phrase}を自然に反映。"
        else:
            tone = f"{tone_phrase}を自然に反映。"

    cleaned_reason = _sanitize_emotion_reason_for_prompt(reason)
    if cleaned_reason:
        return f"現在の感情: {emotion_summary}\n理由: {cleaned_reason}\n感情の温度感: {tone}"
    return f"現在の感情: {emotion_summary}\n感情の温度感: {tone}"


def emotion_blend_ratio() -> float:
    return cfg_float("EMOTION_BLEND_RATIO", 0.35)


def emotion_decay() -> float:
    return cfg_float("EMOTION_DECAY", 0.92)


EMOTION_REACTION_EMOJIS = {
    "joy": ("❤️", "✨", "🥳"),
    "anticipation": ("👀", "✨"),
    "anger": ("😠",),
    "disgust": ("😒",),
    "sadness": ("💧", "🥺"),
    "surprise": ("😳", "✨"),
    "fear": ("😨", "👀"),
}


def build_memory_emotion_tags(scores: dict[str, float] | None, *, max_tags: int = 2, min_score: float = 0.34) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        score = clamp_score((scores or {}).get(key, 0.0))
        if score >= min_score:
            ranked.append((key, score))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [key for key, _ in ranked[:max(1, int(max_tags))]]


def format_emotion_tags_ja(tags: list[str] | tuple[str, ...] | None) -> str:
    items = [EMOTION_LABEL_TO_JA.get(str(tag), str(tag)) for tag in (tags or []) if str(tag).strip()]
    return "・".join(items)


def choose_emotion_reaction_emojis(
    scores: dict[str, float] | None,
    *,
    threshold: float = 0.80,
    max_emojis: int = 2,
) -> list[str]:
    ranked: list[tuple[str, float]] = []
    for key in EMOTION_KEYS:
        score = clamp_score((scores or {}).get(key, 0.0))
        if score >= threshold:
            ranked.append((key, score))
    ranked.sort(key=lambda item: item[1], reverse=True)

    chosen: list[str] = []
    for key, _ in ranked:
        for emoji in EMOTION_REACTION_EMOJIS.get(key, ()):
            if emoji not in chosen:
                chosen.append(emoji)
            if len(chosen) >= max_emojis:
                return chosen
    return chosen
