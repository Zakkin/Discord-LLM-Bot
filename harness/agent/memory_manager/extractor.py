"""
[AI Agent Summary]
長期記憶の抽出、ハルシネーション遮断ガード（項目87）、メモリ検証およびDB保存を担当するモジュール。
This module handles long-term memory extraction, hallucination blocking guards, memory validation, and storage.
"""
from __future__ import annotations

import difflib
import logging
import re
import time
from typing import Any

from .compat import get_ollama_helpers, json_loads
from .store import MemoryStore
from .types import MemoryRecord
from ollama_bot.common.emotion_helpers import (
    EMOTION_KEYS,
    EMOTION_LABEL_TO_JA,
    build_memory_emotion_tags,
    format_emotion_tags_ja,
)
from ollama_bot.common import ollama_helpers
from ollama_bot.common.bot_identity import _extract_character_names_from_config

log = logging.getLogger("harness.agent.memory_manager.extractor")
logger = log

_ALLOWED_ASSOCIATED_EMOTIONS = {*(str(key) for key in EMOTION_KEYS), "neutral", "none"}
_ASSOCIATED_EMOTION_ALIASES = {
    **{str(label): key for key, label in EMOTION_LABEL_TO_JA.items()},
    "なし": "none",
    "無し": "none",
    "無": "none",
    "普通": "neutral",
    "平常": "neutral",
}

MEMORY_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_store": {"type": "boolean"},
        "memories": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "memory_type": {"type": "string"},
                    "content": {"type": "string"},
                    "associated_emotion": {"type": "string"},
                    "score": {"type": "number"},
                },
                "required": ["memory_type", "content", "associated_emotion", "score"],
                "additionalProperties": False,
            },
        },
        "training_candidate": {
            "type": "object",
            "properties": {
                "should_keep": {"type": "boolean"},
                "reason": {
                    "type": "string",
                    "description": "保存・学習候補とする理由。主語は曖昧な『ユーザー』ではなく必ず対象のサーバーニックネーム（『〇〇』さん）を用いること",
                },
            },
            "required": ["should_keep", "reason"],
            "additionalProperties": False,
        },
    },
    "required": ["should_store", "memories", "training_candidate"],
    "additionalProperties": False,
}

ALLOWED_MEMORY_TYPES = {
    "user_preference", "preference", "fact", "constraint", "setting", "theme",
    "conversation_theme", "communication_style", "behavioral_principle",
    "location_preference", "recipe", "food",
}

BLOCKED_MEMORY_TYPES = {
    "user", "ai", "ai_response", "user_request", "persona", "personality",
    "guild_id", "channel_id", "user_id", "character", "character_name",
    "dialogue", "conversation", "context",
}

_GENERIC_MEMORY_USER_RE = re.compile(r"^『?(?:(?:対象|この|当該)?ユーザー|ユーザ|user)(?:_\d+)?』?$", re.IGNORECASE)


def normalize_memory_text(
    text: str,
    *,
    target_user_name: str | None = None,
    target_user_id: int | str | None = None,
    aliases: list[str] | None = None,
    include_uid_tag: bool = False,
) -> str:
    """記憶理由や記憶テキスト内の主語が曖昧な『ユーザー』等になっている場合に対象サーバーニックネーム/IDに補正し、旧エイリアスを最新名に置換する。"""
    t = str(text or "").strip()
    if not t:
        return ""

    from .user_identity_resolver import replace_aliases_with_current_name, format_user_internal_tag

    clean_name = str(target_user_name or "").strip()
    base_name = ""
    if clean_name:
        if clean_name.startswith(("『", "「")) and clean_name.endswith(("』", "」")):
            clean_name = clean_name[1:-1].strip()
        base_name = re.sub(r"[さんくん君ちゃん氏様]+$", "", clean_name).strip() or clean_name
        if not re.match(r"^ユーザー_\d+$", base_name):
            replacement = f"『{base_name}』さん"
        elif target_user_id:
            replacement = f"ユーザー（ID: {target_user_id}）"
        else:
            replacement = ""
    elif target_user_id:
        replacement = f"ユーザー（ID: {target_user_id}）"
    else:
        replacement = ""

    if replacement:
        # 文頭の主語置換: 「ユーザーが」「ユーザーは」「ユーザーの」「ユーザーにとって」「相手が」など
        prefix_pattern = re.compile(
            r"^(?:(?:対象|この|当該)?ユーザー|(?:会話|対話)?相手|彼|彼女)(?:からの|より|にとって|への|との|は|が|の)"
        )
        m = prefix_pattern.match(t)
        if m:
            matched = m.group(0)
            for particle in ("からの", "より", "にとって", "への", "との", "は", "が", "の"):
                if matched.endswith(particle):
                    rest = t[len(matched):].lstrip()
                    t = f"{replacement}{particle}{rest}"
                    break

        # 文中の「ユーザーが」「ユーザーは」「ユーザーの」「ユーザーにとって」「ユーザーとの」「ユーザーへの」等の置換
        # ただし「ユーザーID」などの技術用語は除外
        t = re.sub(
            r"(?<![A-Za-z0-9_])(?:(?:対象|この|当該)?ユーザー|(?:会話|対話)?相手)(?=(?:からの|より|[はがの]|にとって|との|への))",
            replacement,
            t,
        )

    # base_name が有効（かつ ユーザー_\d+ ではない）な場合、テキスト中の 『?ユーザー_\d+』?(?:さん|くん|ちゃん|氏|様)? を replacement に置換
    if base_name and not re.match(r"^ユーザー_\d+$", base_name) and replacement:
        t = re.sub(r"『?ユーザー_\d+』?(?:さん|くん|君|ちゃん|氏|様)?", replacement, t)

    # 過去のエイリアス（旧ニックネーム）を最新サーバーニックネームに置換
    if clean_name and aliases and not re.match(r"^ユーザー_\d+$", base_name):
        t = replace_aliases_with_current_name(t, current_name=clean_name, aliases=aliases)

    # 内部処理用タグ [uid:...] の付与（要求時かつ未付与時）
    if include_uid_tag and target_user_id:
        uid_tag = format_user_internal_tag(target_user_id)
        if uid_tag not in t:
            t = f"{uid_tag} {t}"

    return t


def _normalize_score(value: Any) -> float:
    try:
        score = float(value)
    except Exception:
        return 0.0
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _is_self_referential_memory(content: str) -> bool:
    """AI自身（設定されたBot呼称）のキャラクター設定・プロフィール・状態をユーザー記憶として保存・注入しないための判定。"""
    text = str(content or "").strip()
    if not text:
        return False

    try:
        char_names = _extract_character_names_from_config()
        if char_names:
            escaped_names = "|".join(re.escape(n) for n in char_names if len(n) >= 2)
            if escaped_names:
                dynamic_pattern = rf"(?:^|[「『（(\s])(?:{escaped_names})(?:は|が|の|って|という|について|も)"
                if re.search(dynamic_pattern, text, flags=re.IGNORECASE):
                    return True
    except Exception:
        pass

    generic_subject_pattern = r"(?:^|[「『（(\s])(?:AI|ai|Bot|bot|ボット|アシスタント)(?:は|が|の|って|という|について|も)"
    if re.search(generic_subject_pattern, text, flags=re.IGNORECASE):
        return True

    return False


def _looks_like_noise_memory(content: str) -> bool:
    text = str(content or "").strip()
    if not text or len(text) < 8 or text.isdigit():
        return True
    if re.fullmatch(r"<@!?\d+>", text) or re.fullmatch(r"\d{15,25}", text) or _is_self_referential_memory(text):
        return True
    lowered = text.lower()
    blocked_fragments = ("guild_id", "channel_id", "user_id", "persona", "ai_response", "bot返答", "ユーザー発言", "高id:")
    return any(frag in lowered for frag in blocked_fragments)


def _is_assistant_quote_like(content: str, assistant_text: str, user_text: str = "") -> bool:
    c = str(content or "").strip()
    a = str(assistant_text or "").strip()
    if not c or not a:
        return False
    normalized_content = re.sub(r"\s+", " ", c)
    normalized_assistant = re.sub(r"\s+", " ", a)
    normalized_user = re.sub(r"\s+", " ", str(user_text or "").strip())
    if normalized_content == normalized_assistant:
        return True

    if normalized_user:
        if normalized_content in normalized_user:
            return False
        user_tokens = set(re.findall(r'[A-Za-z0-9_]{2,}|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]{2,}', normalized_user))
        content_tokens = set(re.findall(r'[A-Za-z0-9_]{2,}|[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFFー]{2,}', normalized_content))
        if user_tokens and (user_tokens & content_tokens):
            return False

    if len(normalized_content) < 24 and normalized_content in normalized_assistant:
        return True
    return bool(
        normalized_assistant in normalized_content
        and len(normalized_content) <= len(normalized_assistant) + 12
    )


_UNSAFE_SEXUAL_OR_LEWD_RE = re.compile(
    r"(?:"
    r"下ネタ|おっぱい|オッパイ|巨乳|貧乳|爆乳|ちっぱい|乳首|母乳|"
    r"おまなんこ|おまんこ|ちんこ|まんこ|ちんぽ|チンコ|マンコ|チンポ|ペニス|ヴァギナ|"
    r"金玉|睾丸|性器|陰部|陰茎|クリトリス|アナル|肛門|"
    r"勃起|射精|絶頂|潮吹き|中出し|精液|ザーメン|精子|"
    r"オナニー|マスターベーション|自慰|セックス|性行為|性交|獣姦|近親相姦|"
    r"風俗|ソープランド|デリヘル|アダルトビデオ|AV女優|"
    r"セクハラ|性的虐待|性的暴行|淫乱|露出狂|パンチラ|ノーパン|ノーブラ|パイパン|包茎|短小|早漏|インポ|"
    r"(?<![ァ-ヶー])(?:エロい|エロすぎ|エロ画|エロ動|エロ本|エロゲ|エロネタ|エロ話|エロ漫画|エロ同人|エロサイト|ドエロ|エロ(?![イカージョン]))"
    r")",
    re.IGNORECASE,
)

_UNSAFE_BODY_SHAMING_OR_BANTER_RE = re.compile(
    r"(?:"
    r"自分のおっぱい|自分の乳|おっぱい.*吸える|吸えるほど太|"
    r"太って(?:いる|る|て)|太りすぎ|激太り|肥満体型|ビール腹|太鼓腹|二重あご|豚みたい|ブタみたい|"
    r"(?<![ァ-ヶー])(?:ドデブ|デブ(?:だ|すぎ|専|体型|キャラ|って|ってる|っている)|デブ(?![ーリ]))|"
    r"(?<![ァ-ヶー])(?:ハゲ(?:て|てる|ている|頭|散らか|上が|すぎ)|若ハゲ|M字ハゲ|薄毛|ハゲ(?![タカワシイトウ山]))|"
    r"(?<![ァ-ヶー])(?:ドブス|ブス(?:だ|すぎ|女|顔|専)|ブサイク|不細工)(?![ァ-ヶー])|"
    r"ドチビ|チビすぎ|短足すぎ|顔がでかい|顔がデカい|ワキガ|体臭が臭|口臭が臭"
    r")",
    re.IGNORECASE,
)


def _get_unsafe_or_banter_filter_reason(content: str, memory_type: str = "") -> str | None:
    """下世話な性的表現・身体部位ネタや第三者への体型・外見揶揄、暴言等のフィルタ理由を返す。"""
    text = str(content or "").strip()
    if not text:
        return None
    if _UNSAFE_SEXUAL_OR_LEWD_RE.search(text):
        return "sexual_or_lewd_expression"
    if any(token in text for token in ("死んだ息子", "子作り", "子供できちゃった", "どっちが子供産む")):
        return "inappropriate_family_banter"
    if re.search(r"(?:非難した|腹を立てた|指摘し.*腹を|国語力|国語すら|罵倒|暴言|無能|使えない)", text):
        return "hostile_or_abuse_expression"
    if _UNSAFE_BODY_SHAMING_OR_BANTER_RE.search(text):
        return "body_shaming_or_appearance_banter"
    return None


def _is_single_turn_unsafe_or_low_value(content: str, memory_type: str = "") -> bool:
    return _get_unsafe_or_banter_filter_reason(content, memory_type=memory_type) is not None


_MEMORY_HALLUCINATION_STOPWORDS = {
    "ユーザー", "メンバー", "アシスタント", "ボット", "自分", "相手", "発言", "会話",
    "日常", "話題", "設定", "情報", "共有", "予定", "約束", "現在", "以前", "今後",
    "最近", "今日", "昨日", "明日", "こと", "もの", "ため", "わけ", "よう", "そう",
    "好き", "嫌い", "愛用", "持っ", "言っ", "聞い", "知っ", "思っ", "感じ", "反応",
    "質問", "回答", "返答", "相談", "依頼", "確認", "話す", "言う", "食べる", "飲む",
    "言い方", "話し方", "態度", "様子", "言葉", "対応",
}

_COMMON_HIRAGANA_TOPIC_WORDS = {
    "おにぎり", "おむすび", "ごはん", "うどん", "そば", "お菓子", "おやつ", "あんこ",
    "くだもの", "たまご", "さかな", "おにく", "みかん", "りんご", "いちご", "すいか",
    "ぶどう", "トマト", "パン", "ケーキ", "パスタ", "カレー", "ラーメン",
}


def _extract_content_meaningful_tokens(
    text: str,
    target_user_name: str | None = None,
) -> set[str]:
    """記憶文から主語呼称を除去し、実質的な名詞・キーワード（漢字・カタカナ・英数字・クォート語句・特定ひらがな名詞）を抽出する。"""
    s = str(text or "").strip()
    if not s:
        return set()

    # 1. 内部IDタグ [uid:...] の除去
    s = re.sub(r"\[uid:[^\]]+\]", "", s)

    # 2. ユーザー呼称・主語の除去（文頭および文中の『...』さん、「...」さん、...さん等）
    s = re.sub(r"^[^（(「\s]{1,30}[（(]ユーザー[）)][はがのをに、\s]*", "", s)
    s = re.sub(r"^[^\s:：]{1,20}(?:さん|くん|君|ちゃん|氏|様)[はがのをに、\s]*", "", s)
    s = re.sub(r"^ユーザー[はがのをに、\s]*", "", s)

    # ターゲットユーザー名が明示されている場合、その名称を記憶文から除去しストップワードに追加
    name_stopwords: set[str] = set()
    if target_user_name:
        clean_target = str(target_user_name).strip()
        clean_target = re.sub(r"[さんくん君ちゃん氏様]+$", "", clean_target).strip()
        if clean_target.startswith(("『", "「")) and clean_target.endswith(("』", "」")):
            clean_target = clean_target[1:-1].strip()
        if clean_target:
            name_stopwords.add(clean_target)
            s = s.replace(f"『{clean_target}』", "").replace(f"「{clean_target}」", "").replace(clean_target, "")

    # 文中の『...』さん、「...」さんも除去
    s = re.sub(r"[『「][^』」]+[』」](?:さん|くん|君|ちゃん|氏|様)?", "", s)

    # クォート『…』「…」で括られた重要語（例: 『ばか』等）を救出（原文から抽出、ただし名前やストップワードは除外）
    quoted_phrases = set(re.findall(r'[「『]([^」』]+)[」』]', text))
    quoted_tokens = {
        q.strip()
        for q in quoted_phrases
        if 2 <= len(q.strip()) <= 20
        and q.strip() not in _MEMORY_HALLUCINATION_STOPWORDS
        and q.strip() not in name_stopwords
    }

    raw_tokens = set(re.findall(r'[\u4E00-\u9FFF]{2,}|[\u30A0-\u30FFー]{2,}|[A-Za-z0-9_]{3,}', s))
    for hw in _COMMON_HIRAGANA_TOPIC_WORDS:
        if hw in s:
            raw_tokens.add(hw)

    raw_tokens |= quoted_tokens
    return {t for t in raw_tokens if t not in _MEMORY_HALLUCINATION_STOPWORDS and t not in name_stopwords}


def _is_assistant_context_line(line: str) -> bool:
    """文脈ログの1行がAssistant（Bot自身）の発言行であるかを判定する。"""
    raw = str(line or "").strip()
    if not raw:
        return False
    if re.match(r"^(?:assistant|ai|bot)[:：]", raw, flags=re.IGNORECASE):
        return True
    char_names = _extract_character_names_from_config()
    for name in char_names:
        if name and re.match(rf"^{re.escape(name)}[:：]", raw, flags=re.IGNORECASE):
            return True
    return False


def _build_dialogue_source(
    user_text: str = "",
    assistant_text: str = "",
    context_lines: list[str] | None = None,
    *,
    exclude_assistant: bool = False,
) -> str:
    parts: list[str] = [str(user_text or "")]
    if not exclude_assistant and assistant_text:
        parts.append(str(assistant_text))
    if context_lines:
        for line in context_lines:
            raw = str(line or "").strip()
            if not raw:
                continue
            if exclude_assistant and _is_assistant_context_line(raw):
                continue
            parts.append(raw)
    return " ".join(parts)


def _is_swapped_user_preference_memory(
    content: str,
    user_text: str,
    assistant_text: str,
    context_lines: list[str] | None = None,
    *,
    memory_type: str = "",
    is_reply_to_other_user: bool = False,
    target_user_name: str | None = None,
) -> bool:
    """
    ユーザー嗜好（preference / user_preference）のソース検証ガード新設（多重防御）。

    1. memory_type が preference または user_preference の場合、記憶内容（content）の主要トークン
       （名詞・動詞・形容詞）が user_text（および直近の同一ユーザー発言文脈）に最低1つ以上含まれていることを検証する。
    2. 主要トークンが assistant_text（AI自身の返答）にしか存在せず、user_text に存在しない場合は、
       「AI自身の発言・主張をユーザーの好みにすり替えたハルシネーション記憶」として判定し、True（破棄対象）を返す。
    3. 他者宛て返信（Reply）の場合、返信先引用文のみに含まれ本人の発言本文に含まれない話題を
       本人の好みとして誤認保存しようとした場合は True（破棄対象）を返す。
    """
    if memory_type not in ("preference", "user_preference", "food", "location_preference"):
        return False

    # 他者宛て返信文脈における親発言の話題誤認ガード
    if is_reply_to_other_user or ("への返信）" in user_text and not ("あなたの" in user_text or "Assistant" in user_text)):
        m_quote = re.search(r"^[（(].+?の「(.+?)」への返信[）)]", user_text, flags=re.DOTALL)
        if m_quote:
            quote_text = m_quote.group(1).strip()
            user_pure_text = user_text[m_quote.end():].strip()
            quote_tokens = _extract_content_meaningful_tokens(quote_text, target_user_name=target_user_name)
            user_pure_tokens = _extract_content_meaningful_tokens(user_pure_text, target_user_name=target_user_name)
            quote_only_tokens = {t for t in quote_tokens if t not in user_pure_tokens}
            meaningful_content_tokens = _extract_content_meaningful_tokens(content, target_user_name=target_user_name)
            if any(t in quote_only_tokens for t in meaningful_content_tokens) and not any(t in user_pure_tokens for t in meaningful_content_tokens):
                log.warning(
                    "_is_swapped_user_preference_memory: memory %r contains quote-only tokens from replied user in reply context (quote_only=%r)",
                    content,
                    quote_only_tokens,
                )
                return True

    meaningful_tokens = _extract_content_meaningful_tokens(content, target_user_name=target_user_name)
    if not meaningful_tokens:
        return True

    user_source = _build_dialogue_source(
        user_text,
        assistant_text="",
        context_lines=context_lines,
        exclude_assistant=True,
    )
    user_source_tokens = _extract_content_meaningful_tokens(user_source, target_user_name=target_user_name)

    # 1. 記憶の主要トークンがユーザー発言ソース（user_text および直近文脈）に最低1つ以上含まれているか
    if not any(token in user_source for token in meaningful_tokens):
        log.warning(
            "_is_swapped_user_preference_memory: no meaningful token found in user source for memory %r (tokens=%r, user_source=%r)",
            content,
            meaningful_tokens,
            user_source,
        )
        return True

    # 2. 主要トークンが assistant_text（AI自身の返答）にしか存在せず、user_text に存在しないかを検証
    if assistant_text:
        assistant_tokens = _extract_content_meaningful_tokens(assistant_text, target_user_name=target_user_name)
        assistant_only_tokens = {t for t in assistant_tokens if t not in user_source}
        swapped = {t for t in meaningful_tokens if t in assistant_only_tokens}
        if swapped:
            log.warning(
                "_is_swapped_user_preference_memory: assistant-only tokens %r found in user preference memory %r",
                swapped,
                content,
            )
            return True

    return False


def _is_unsupported_hallucinated_memory(
    content: str,
    user_text: str,
    assistant_text: str,
    context_lines: list[str] | None = None,
    *,
    memory_type: str = "",
    target_user_name: str | None = None,
) -> bool:
    """記憶内容の主要キーワードが、会話（ユーザー発言、AI返答、直近文脈）のいずれにも一切含まれない捏造記憶かを判定する（項目87）。

    memory_type が preference または user_preference の場合は、AI自身の主張やセリフからの主客すり替えを防ぐため、
    根拠トークンの照合対象を assistant_text を除外した user_text（および直前同一ユーザー文脈）に限定する。
    """
    meaningful_tokens = _extract_content_meaningful_tokens(content, target_user_name=target_user_name)
    if not meaningful_tokens:
        return True
    exclude_assistant = memory_type in ("preference", "user_preference", "food", "location_preference")
    source = _build_dialogue_source(
        user_text,
        assistant_text,
        context_lines,
        exclude_assistant=exclude_assistant,
    )
    if not source.strip():
        return True
    if not any(token in source for token in meaningful_tokens):
        return True

    # preference / user_preference の場合、AI返答にしか存在しないトークン（AI独自のセリフ・反論・マナー主張等）が
    # ユーザーの嗜好として混入している場合は主客すり替えとして遮断する（多重防御）
    if exclude_assistant and assistant_text:
        assistant_tokens = _extract_content_meaningful_tokens(assistant_text, target_user_name=target_user_name)
        user_source_tokens = _extract_content_meaningful_tokens(source, target_user_name=target_user_name)
        assistant_only_tokens = assistant_tokens - user_source_tokens
        if meaningful_tokens & assistant_only_tokens:
            return True

    return False


def _is_hallucinated_emotion_reason_memory(
    content: str,
    emotion_reason: str | None,
    user_text: str = "",
    assistant_text: str = "",
    context_lines: list[str] | None = None,
) -> bool:
    """記憶内容が、過去の感情理由（emotion_reason）から引き写され、かつ実際の会話に存在しない捏造かを判定する（項目87）。"""
    reason = str(emotion_reason or "").strip()
    if not reason or not content:
        return False
    reason_tokens = _extract_content_meaningful_tokens(reason)
    if not reason_tokens:
        return False
    overlap = reason_tokens & _extract_content_meaningful_tokens(content)
    if not overlap:
        return False
    source = _build_dialogue_source(user_text, assistant_text, context_lines)
    return any(t not in source for t in overlap)


def _is_unconfirmed_factcheck_fabrication(
    content: str,
    assistant_text: str = "",
    context_lines: list[str] | None = None,
    *,
    memory_type: str = "",
) -> bool:
    """未確認・根拠なしとされたファクトチェック話題に関して、AIの推測や架空公式見解をfact保存しようとしていないか判定する。"""
    if memory_type != "fact":
        return False
    source = " ".join([str(assistant_text or ""), *(str(line or "") for line in (context_lines or []))])
    if not any(marker in source for marker in ("根拠が確認でき", "根拠は見当たり", "確認できません", "情報は見当たり", "見当たらない", "確認できなかった")):
        return False
    fabrication_markers = ("公式に", "名称由来", "の由来は", "名前の由来", "子会社で", "子会社であ")
    return any(m in content for m in fabrication_markers)


_REPLY_PREFIX_QUOTE_RE = re.compile(r"^[（(][^（(]*?の「.*?」への返信[）)]\s*", flags=re.DOTALL)
_REPLY_PREFIX_GENERIC_RE = re.compile(r"^[（(].+?への返信[）)]\s*", flags=re.DOTALL)
_TIMESTAMP_PREFIX_RE = re.compile(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+")
_SPEAKER_SUFFIX_TAG_RE = re.compile(
    r"\s*(?:\((?:>>[^)]*|独立発言|[^)]*への返信)\)|\[(?:>>[^\]]*|独立発言|[^\]]*への返信)\])\s*$"
)
_SYSTEM_CONTEXT_LINE_RE = re.compile(
    r"^(?:(?:system|sys|メモ|記憶|思考)[:：]|[（(](?:内心のメモ|[^）)]*の記憶・実感)[:：])",
    flags=re.IGNORECASE,
)

# 単なる話題言及・受身・静的存在など、その人物自身を行為者とする帰属ではない述部
_NON_ACTOR_PREDICATE_RE = re.compile(
    r"(?:"
    r"(?:話題|噂|話|名前|名|存在|話題性)に(?:なっ|なり|なる|出|で|上がっ|上がり|上がる|あがっ|あがり|あがる|し|され)(?:た|て|てい(?:る|た))?|"
    r"(?:会話|トーク|スレ|話題|話)に(?:登場|出|で|出て|現れ)(?:し?(?:た|て|てい(?:る|た)))?|"
    r"(?:登場|存在|該当|発生|誕生|所属|在住|出身|在籍|一致|判明|発覚|流行|普及|残存)(?:し(?:た|て|てい(?:る|た)|よう)|する)|"
    r"[\u4E00-\u9FFF\u30A0-\u30FFー]{2,}され(?:た|て|る|てい(?:る|た)|ない)?|"
    r"(?:言わ|聞か|笑わ|怒ら|呼ば|誘わ|頼ま|褒めら|叱ら|勧めら|持ちかけら|持ち掛けら|語ら|触れら|挙げら|あげら|知ら|見ら|思わ|扱わ|書か|描か|奪わ|弄ら|いじら|突っ込ま|ツッコま|振ら)れ(?:た|て|る|てい(?:る|た)|ない)?"
    r")"
)

# 特定の人物を行為者（発言・提案・行動の主体）として帰属する能動述部
_ACTOR_ACTION_PREDICATE_RE = re.compile(
    r"(?:"
    r"[\u4E00-\u9FFF\u30A0-\u30FFー]{2,}(?:し(?:た|て|よう)|する)|"
    r"を(?:し(?:た|て|よう)|する)|"
    r"言っ(?:た|て)|言う|言い(?:出し|出|張っ|張る|返し|返す)|"
    r"話し(?:た|て)|話す|語っ(?:た|て)|語る|述べ(?:た|て|る)|"
    r"持ちかけ(?:た|て|る)|持ち掛け(?:た|て|る)|"
    r"持ち出し(?:た|て)|持ち出す|切り出し(?:た|て)|切り出す|"
    r"出し(?:た|て)|出す|挙げ(?:た|て|る)|あげ(?:た|て|る)|"
    r"聞い(?:た|て)|聞く|尋ね(?:た|て|る)|頼ん(?:だ|で)|頼む|"
    r"教え(?:た|て|る)|勧め(?:た|て|る)|すすめ(?:た|て|る)|誘っ(?:た|て)|誘う|"
    r"呟い(?:た|て)|つぶやい(?:た|て)|叫ん(?:だ|で)|叫ぶ|"
    r"笑っ(?:た|て)|笑う|怒っ(?:た|て)|怒る|喜ん(?:だ|で)|喜ぶ|驚い(?:た|て)|驚く|悲しん(?:だ|で)|嘆い(?:た|て)|ぼやい(?:た|て)|訴え(?:た|て|る)|"
    r"思っ(?:た|て)|思う|考え(?:た|て|る)|感じ(?:た|て|る)|"
    r"突っ込ん(?:だ|で)|突っ込む|ツッコん(?:だ|で)|ツッコむ|ツッコミを入れ|ボケ(?:た|て|る)|茶化し(?:た|て)|茶化す|"
    r"弄っ(?:た|て)|いじっ(?:た|て)|振っ(?:た|て)|振る|乗っ(?:た|て)|乗る|返し(?:た|て)|返す|受け入れ(?:た|て|る)|"
    r"例え(?:た|て|る)|喩え(?:た|て|る)|たとえ(?:た|て|る)|"
    r"食べ(?:た|て|る)|飲ん(?:だ|で)|飲む|"
    r"買っ(?:た|て)|買う|売っ(?:た|て)|売る|使っ(?:た|て)|使う|"
    r"作っ(?:た|て)|作る|書い(?:た|て)|書く|描い(?:た|て)|描く|撮っ(?:た|て)|撮る|貼っ(?:た|て)|貼る|"
    r"読ん(?:だ|で)|読む|見(?:た|て|る)|観(?:た|て|る)|"
    r"行っ(?:た|て)|行く|(?<![出])来(?:た|て|る)|帰っ(?:た|て)|帰る|出かけ(?:た|て|る)|"
    r"遊ん(?:だ|で)|遊ぶ|やっ(?:た|て)|やる|決め(?:た|て|る)|選ん(?:だ|で)|選ぶ|"
    r"始め(?:た|て|る)|続け(?:た|て|る)|終え(?:た|て|る)|やめ(?:た|て|る)|"
    r"試し(?:た|て)|試す|調べ(?:た|て|る)|探し(?:た|て)|見つけ(?:た|て|る)|待っ(?:た|て)|会っ(?:た|て)|"
    r"送っ(?:た|て)|渡(?:し(?:た|て)|す)|受け(?:た|て|る)|拾っ(?:た|て)|捨て(?:た|て|る)|"
    r"触れ(?:た|て|る)|乗せ(?:た|て|る)|載せ(?:た|て|る)"
    r")"
)


def _get_bot_actor_names() -> set[str]:
    """行為者チェック時に除外する Bot 自身の呼称（小文字正規化済み）集合を返す。"""
    names = {"ai", "bot", "assistant", "アシスタント", "ボット", "system", "sys"}
    try:
        for char_name in _extract_character_names_from_config():
            clean = str(char_name or "").strip().lower()
            if clean:
                names.add(clean)
                names.add(f"ai{clean}")
    except Exception:
        pass
    return names


def _clean_reply_prefix(text: str) -> str:
    """発言先頭の（〇〇の「...」への返信）プレフィックスを除去し、本人の発言本文のみを返す。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s2 = _REPLY_PREFIX_QUOTE_RE.sub("", s, count=1).strip()
    if s2 != s:
        return s2
    return _REPLY_PREFIX_GENERIC_RE.sub("", s, count=1).strip()


def _extract_human_speaker_utterances(
    *,
    user_text: str,
    assistant_text: str,
    context_lines: list[str] | None = None,
    target_user_name: str | None = None,
) -> tuple[dict[str, list[str]], str]:
    """対話相手および文脈内の各人間話者ごとに本人の発言テキストを集め、Bot 発言ソースと分けて返す。"""
    bot_names = _get_bot_actor_names()
    speaker_parts: dict[str, list[str]] = {}
    bot_parts: list[str] = []
    if assistant_text:
        bot_parts.append(str(assistant_text))

    target_label = _clean_speaker_label_for_provenance(target_user_name)
    if target_label and target_label.lower() not in bot_names:
        clean_user = _clean_reply_prefix(user_text)
        speaker_parts.setdefault(target_label, [])
        if clean_user:
            speaker_parts[target_label].append(clean_user)

    for raw_line in context_lines or []:
        raw = str(raw_line or "").strip()
        if not raw:
            continue
        if _is_assistant_context_line(raw):
            bot_parts.append(raw)
            continue
        stripped = _TIMESTAMP_PREFIX_RE.sub("", raw).strip()
        if not stripped or _is_assistant_context_line(stripped) or _SYSTEM_CONTEXT_LINE_RE.match(stripped):
            if stripped and _is_assistant_context_line(stripped):
                bot_parts.append(stripped)
            continue
        m_col = re.match(r"^([^:\n：]{1,60})[:：]\s*(.*)$", stripped, flags=re.DOTALL)
        if not m_col:
            continue
        sp_raw, body = m_col.group(1).strip(), m_col.group(2).strip()
        if sp_raw.lower() in ("http", "https", "ftp") or body.startswith("//"):
            continue
        sp = _SPEAKER_SUFFIX_TAG_RE.sub("", sp_raw).strip()
        if sp.lower() in bot_names or _is_assistant_context_line(f"{sp}:"):
            bot_parts.append(body or stripped)
            continue
        clean_sp = _clean_speaker_label_for_provenance(sp)
        if clean_sp and clean_sp.lower() not in bot_names:
            clean_body = _clean_reply_prefix(body)
            speaker_parts.setdefault(clean_sp, [])
            if clean_body:
                speaker_parts[clean_sp].append(clean_body)

    return speaker_parts, " ".join(bot_parts)


def _split_sentences_preserving_quotes(text: str) -> list[str]:
    """引用符「…」/『…』内の句読点では分割せず、文単位に分割する。"""
    s = str(text or "").strip()
    if not s:
        return []
    sentences: list[str] = []
    pairs = {"「": "」", "『": "』"}
    stack: list[str] = []
    start = 0
    for i, ch in enumerate(s):
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif not stack and ch in "。！？\n":
            part = s[start:i].strip()
            if part:
                sentences.append(part)
            start = i + 1
    tail = s[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _extract_unquoted_and_quote_spans(
    sentence: str,
    speaker_label: str,
) -> tuple[str, list[tuple[str, int]]]:
    """文から話者自身の囲み括弧を保持しつつ、それ以外の引用句「…」/『…』をマスクした地の文と引用リストを返す。"""
    s = str(sentence or "")
    if not s:
        return "", []
    speaker_bracket_re = (
        re.compile(
            rf"^(?:[『「]\s*{re.escape(speaker_label)}(?:さん|くん|君|ちゃん|氏|様)?\s*[』」])"
        )
        if speaker_label
        else None
    )

    out_chars: list[str] = []
    quotes: list[tuple[str, int]] = []
    pairs = {"「": "」", "『": "』"}
    stack: list[tuple[str, int]] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if not stack and speaker_bracket_re is not None:
            m_sp = speaker_bracket_re.match(s[i:])
            if m_sp:
                token = m_sp.group(0)
                out_chars.append(token)
                i += len(token)
                continue
        if ch in pairs:
            stack.append((pairs[ch], i))
            if len(stack) == 1:
                out_chars.append(ch)
            i += 1
            continue
        if stack and ch == stack[-1][0]:
            _, start_idx = stack.pop()
            if not stack:
                quotes.append((s[start_idx + 1 : i], start_idx))
                out_chars.append(ch)
            i += 1
            continue
        if not stack:
            out_chars.append(ch)
        i += 1

    if stack:
        _, unclosed_start = stack[0]
        out_chars.append(s[unclosed_start + 1 :])

    return "".join(out_chars), quotes


def _build_speaker_subject_regex(label: str) -> re.Pattern[str]:
    esc = re.escape(label)
    bracketed = rf"[『「]\s*{esc}(?:さん|くん|君|ちゃん|氏|様)?\s*[』」](?:さん|くん|君|ちゃん|氏|様)?"
    if len(label) >= 2:
        unbracketed = rf"(?<![\u4E00-\u9FFF\u30A0-\u30FFA-Za-z0-9_]){esc}(?:さん|くん|君|ちゃん|氏|様)?"
    else:
        unbracketed = rf"(?<![\u4E00-\u9FFF\u30A0-\u30FFA-Za-z0-9_]){esc}(?:さん|くん|君|ちゃん|氏|様)"
    return re.compile(rf"(?:{bracketed}|{unbracketed})(?:自身|本人)?[はがも]")


def _is_actor_attributed_in_sentence(
    sentence: str,
    speaker_label: str,
    *,
    bot_names: set[str],
    all_human_labels: list[str],
) -> bool:
    """1文の中で指定した人間話者が単なる言及ではなく行為・発言の行為者（主語）として帰属されているかを判定する。"""
    unquoted, _ = _extract_unquoted_and_quote_spans(sentence, speaker_label)
    subject_re = _build_speaker_subject_regex(speaker_label)
    other_subject_names = [re.escape(n) for n in sorted(bot_names | set(all_human_labels), key=len, reverse=True) if n and n != speaker_label]
    other_subject_re = (
        re.compile(
            rf"(?:[『「]\s*(?:{'|'.join(other_subject_names)})(?:さん|くん|君|ちゃん|氏|様)?\s*[』」](?:さん|くん|君|ちゃん|氏|様)?|"
            rf"(?<![\u4E00-\u9FFF\u30A0-\u30FFA-Za-z0-9_])(?:{'|'.join(other_subject_names)})(?:さん|くん|君|ちゃん|氏|様)?)[はがも]",
            flags=re.IGNORECASE,
        )
        if other_subject_names
        else None
    )

    for m in subject_re.finditer(unquoted):
        tail = unquoted[m.end():]
        non_actor_match = _NON_ACTOR_PREDICATE_RE.search(tail)
        scoped_tail = tail
        if non_actor_match and other_subject_re is not None:
            m_other = other_subject_re.search(tail, pos=non_actor_match.end())
            if m_other:
                scoped_tail = tail[: m_other.start()]
        cleaned_tail = _NON_ACTOR_PREDICATE_RE.sub(" ", scoped_tail)
        if _ACTOR_ACTION_PREDICATE_RE.search(cleaned_tail):
            return True
    return False


def _token_in_actor_source(token: str, source: str) -> bool:
    if not token or not source:
        return False
    if token in source:
        return True
    if token.isascii():
        return token.lower() in source.lower()
    return False


def _extract_actor_check_tokens(
    sentence: str,
    *,
    quotes: list[tuple[str, int]],
    target_user_name: str | None = None,
    all_human_labels: list[str] | None = None,
) -> set[str]:
    """行為者裏付け検査用に、文全体と21文字以上の引用句内の主要トークンを統合抽出する。"""
    tokens = set(_extract_content_meaningful_tokens(sentence, target_user_name=target_user_name))
    for quote_inner, _ in quotes:
        tokens |= _extract_content_meaningful_tokens(quote_inner, target_user_name=target_user_name)
    for label in all_human_labels or []:
        tokens.discard(label)
    return tokens


def _is_ungrounded_actor_attributed_memory(
    content: str,
    user_text: str,
    assistant_text: str,
    context_lines: list[str] | None = None,
    *,
    target_user_name: str | None = None,
) -> bool:
    """特定の人間を行為者として帰属するメモリに対し、その人物自身の発言による裏付けがあるかを決定的に検証する。

    - 適用対象は、文脈話者または対話相手のクリーン済み話者ラベルが内容に現れ、行為・発言が帰属されている文のみ。
    - 対話相手本人の照合対象は user_text（および同一話者の文脈行）、他話者は文脈行のうちその話者の行（Assistant 行は除外）。
    - Bot 自身を行為者とするメモリおよび単なる言及（話題に出ただけ等）は対象外（False）。
    """
    speaker_parts, bot_source = _extract_human_speaker_utterances(
        user_text=user_text,
        assistant_text=assistant_text,
        context_lines=context_lines,
        target_user_name=target_user_name,
    )
    if not speaker_parts:
        return False

    bot_names = _get_bot_actor_names()
    candidate_labels = sorted(speaker_parts.keys(), key=len, reverse=True)
    bot_quote_owner_re = re.compile(
        rf"(?:{'|'.join(re.escape(n) for n in sorted(bot_names, key=len, reverse=True))}|相手)\s*(?:の|による)$",
        flags=re.IGNORECASE,
    )

    for sentence in _split_sentences_preserving_quotes(content):
        attributed_actors = [
            label
            for label in candidate_labels
            if _is_actor_attributed_in_sentence(
                sentence,
                label,
                bot_names=bot_names,
                all_human_labels=candidate_labels,
            )
        ]
        if not attributed_actors:
            continue

        actor_source = " ".join(
            part
            for label in attributed_actors
            for part in speaker_parts.get(label, [])
        )
        _, quotes = _extract_unquoted_and_quote_spans(sentence, attributed_actors[0])
        tokens = _extract_actor_check_tokens(
            sentence,
            quotes=quotes,
            target_user_name=target_user_name,
            all_human_labels=candidate_labels,
        )
        if not tokens:
            continue

        if not any(_token_in_actor_source(tok, actor_source) for tok in tokens):
            return True

        # 直接引用句「...」（Bot所有表現が直前にないもの）の主要トークンが本人発言に一切なく Bot 発言にある場合は主客逆転とみなす
        if bot_source:
            for quote_inner, quote_start in quotes:
                prefix_before_quote = sentence[max(0, quote_start - 20):quote_start].strip()
                if bot_quote_owner_re.search(prefix_before_quote):
                    continue
                quote_tokens = _extract_content_meaningful_tokens(quote_inner, target_user_name=target_user_name)
                for label in candidate_labels:
                    quote_tokens.discard(label)
                if quote_tokens and not any(_token_in_actor_source(tok, actor_source) for tok in quote_tokens):
                    if any(_token_in_actor_source(tok, bot_source) for tok in quote_tokens):
                        return True

    return False


def _memory_content_bigrams(text: str) -> set[str]:
    """空白除去した記憶テキストの文字バイグラム集合を返す。"""
    normalized = re.sub(r"\s+", "", str(text or ""))
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _is_near_duplicate_memory(content: str, seen_contents: list[str]) -> bool:
    """同一抽出内で既に受理済みの記憶群と近似重複しているか判定する（15文字未満は免除）。"""
    target = re.sub(r"\s+", "", str(content or ""))
    if len(target) < 15:
        return False
    target_bigrams = _memory_content_bigrams(target)
    for seen in seen_contents:
        seen_norm = re.sub(r"\s+", "", str(seen or ""))
        if len(seen_norm) < 15:
            continue
        if target in seen_norm or seen_norm in target:
            return True
        seen_bigrams = _memory_content_bigrams(seen_norm)
        union = target_bigrams | seen_bigrams
        if union and len(target_bigrams & seen_bigrams) / len(union) >= 0.75:
            return True
        if difflib.SequenceMatcher(None, target, seen_norm).ratio() >= 0.80:
            return True
    return False


_UNKNOWN_MEANING_RE = re.compile(
    r"(意味|由来|読み方|正式な意味)[^。]{0,16}(不明|分からな|わからな|未確認)"
)


def _states_unknown_meaning(content: str) -> bool:
    """記憶文自体が対象語の意味・由来・読み方等の不明・未確認を明記しているかを判定する。"""
    return bool(_UNKNOWN_MEANING_RE.search(str(content or "")))


def _extract_provenance_tokens(
    text: str,
    *,
    target_user_name: str | None = None,
) -> set[str]:
    """出所照合用に、既存の主要トークン抽出に加えて数値（0.775等）やクォート語句を補完抽出する。"""
    base_tokens = set(_extract_content_meaningful_tokens(text, target_user_name=target_user_name))
    s = re.sub(r"\[uid:[^\]]+\]", "", str(text or ""))
    s = re.sub(r"<@!?\d+>", "", s)
    # 15桁以上のDiscord IDを除き、小数や数値（例: 0.775, 42等）を抽出
    for num in re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", s):
        if len(num.replace(".", "")) < 15:
            base_tokens.add(num)
    return base_tokens


def _clean_speaker_label_for_provenance(raw_name: str | None) -> str:
    s = str(raw_name or "").strip()
    if not s:
        return ""
    if s.startswith(("『", "「")) and s.endswith(("』", "」")):
        s = s[1:-1].strip()
    s = re.sub(r"(?:さん|くん|君|ちゃん|氏|様)$", "", s).strip()
    if not s or _GENERIC_MEMORY_USER_RE.match(s) or s.lower() in ("user", "ユーザー", "相手"):
        return ""
    return s


def _find_human_speaker_and_quote(
    matched_tokens: set[str],
    *,
    user_text: str,
    context_lines: list[str] | None,
    target_user_name: str | None,
    max_quote_chars: int = 120,
) -> tuple[str, str]:
    """裏付けトークンを含む人間発言の話者名と冒頭抜粋（最大 max_quote_chars 字）を特定する。"""
    clean_user = re.sub(r"^[（(].+?への返信[）)]\s*", "", str(user_text or "").strip(), flags=re.DOTALL).strip()
    speaker_default = _clean_speaker_label_for_provenance(target_user_name)
    if clean_user and any(t in clean_user for t in matched_tokens):
        return speaker_default, clean_user[:max_quote_chars]

    for line in reversed(context_lines or []):
        raw = str(line or "").strip()
        if not raw or _is_assistant_context_line(raw):
            continue
        if not any(t in raw for t in matched_tokens):
            continue
        stripped = re.sub(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+", "", raw).strip()
        if ":" in stripped or "：" in stripped:
            sp, body = re.split(r"[:：]", stripped, maxsplit=1)
            sp = re.sub(
                r"\s*(?:\((?:>>[^)]*|独立発言|[^)]*への返信)\)|\[(?:>>[^\]]*|独立発言|[^\]]*への返信)\])\s*$",
                "",
                sp.strip(),
            ).strip()
            clean_sp = _clean_speaker_label_for_provenance(sp) or speaker_default
            return clean_sp, body.strip()[:max_quote_chars]
        return speaker_default, stripped[:max_quote_chars]

    return speaker_default, (clean_user or str(user_text or "").strip())[:max_quote_chars]


def _classify_memory_provenance(
    content: str,
    *,
    user_text: str,
    assistant_text: str,
    context_lines: list[str] | None = None,
    target_user_name: str | None = None,
    quote_max_chars: int = 120,
) -> tuple[str, str, str]:
    """記憶内容（content）の出所を決定的に分類し、(source_kind, source_speaker, source_quote) を返す。"""
    max_chars = max(1, int(quote_max_chars or 120))
    tokens = _extract_provenance_tokens(content, target_user_name=target_user_name)
    if not tokens:
        return ("unknown", "", "")

    human_source = _build_dialogue_source(
        user_text,
        assistant_text="",
        context_lines=context_lines,
        exclude_assistant=True,
    )
    bot_parts: list[str] = []
    if assistant_text:
        bot_parts.append(str(assistant_text))
    for line in context_lines or []:
        raw = str(line or "").strip()
        if raw and _is_assistant_context_line(raw):
            bot_parts.append(raw)
    bot_source = " ".join(bot_parts)

    in_human = {t for t in tokens if t in human_source}
    in_bot = {t for t in tokens if t in bot_source}

    if in_human:
        kind = "human" if not (in_bot - in_human) else "mixed"
        speaker, quote = _find_human_speaker_and_quote(
            in_human,
            user_text=user_text,
            context_lines=context_lines,
            target_user_name=target_user_name,
            max_quote_chars=max_chars,
        )
        return (kind, speaker, quote)

    if in_bot:
        bot_quote_src = str(assistant_text or "").strip() or bot_source.strip()
        return ("bot_only", "コイト自身", bot_quote_src[:max_chars])

    return ("unknown", "", "")


_FACT_CLAIM_MARKERS = (
    "使われ", "登場する", "に出てくる", "という作品", "という小説", "短編小説", "長編小説",
    "小説", "映画", "漫画", "アニメ", "ゲーム", "曲", "歌", "由来", "出典", "元ネタ",
    "言った", "言っていた", "語っ", "知られ", "数値", "確率", "倍率", "設定", "公式", "事実",
)


def _is_bot_only_ungrounded_fact_claim(
    content: str,
    *,
    memory_type: str,
    source_kind: str,
    target_user_name: str | None = None,
) -> bool:
    """memory_type が fact（または setting / knowledge 系）で、固有名詞・数値・出典主張を含み、
    かつ source_kind == 'bot_only'（人間の発言に裏付けがない Bot 単独主張）であるかを判定する。
    """
    if source_kind != "bot_only":
        return False
    if memory_type not in ("fact", "setting", "knowledge"):
        return False
    text = str(content or "").strip()
    if not text:
        return False

    has_number = bool(re.search(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", text))
    has_quoted_title = bool(re.search(r"[「『][^」』]{1,40}[」』]", text))
    has_proper_or_katakana = bool(re.search(r"[\u30A0-\u30FFー]{2,}|[A-Za-z]{3,}", text))
    has_claim_marker = any(m in text for m in _FACT_CLAIM_MARKERS)
    meaningful = _extract_content_meaningful_tokens(text, target_user_name=target_user_name)

    return bool(has_number or has_quoted_title or has_claim_marker or has_proper_or_katakana or meaningful)


def _is_fallback_reply(cfg: Any, text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    keys = (
        "OLLAMA_MODEL_TIMEOUT_FALLBACK", "OLLAMA_REASONING_LEAK_FALLBACK",
        "OLLAMA_EMPTY_REPLY_FALLBACK", "OLLAMA_PARROT_FALLBACK",
        "OLLAMA_WEATHER_PLACE_REQUIRED_FALLBACK", "OLLAMA_UNKNOWN_FACT_FALLBACK",
        "OLLAMA_UNCLEAR_INTENT_FALLBACK", "OLLAMA_ROLE_FLIP_FALLBACK",
    )
    return any(t == str(cfg(k, "") or "").strip() for k in keys)


def _is_ignorable_call_error(e: Exception) -> bool:
    name = type(e).__name__
    return any(err in name for err in ("Timeout", "Client", "ServerDisconnected", "OSError", "OllamaJSON", "JSONDecode"))


def _normalize_associated_emotion(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in _ALLOWED_ASSOCIATED_EMOTIONS:
        return lowered
    return str(_ASSOCIATED_EMOTION_ALIASES.get(text, "") or "").strip().lower()


def _merge_memory_emotion_tags(turn_tags: list[str], associated_emotion: str) -> list[str]:
    merged: list[str] = []
    norm = _normalize_associated_emotion(associated_emotion)
    if norm and norm not in {"neutral", "none"}:
        merged.append(norm)
    for tag in turn_tags:
        t = _normalize_associated_emotion(tag) or str(tag).strip().lower()
        if t and t not in {"neutral", "none"} and t not in merged:
            merged.append(t)
    return merged


def _join_context_lines(
    context_lines: list[str],
    limit: int = 12,
    *,
    max_chars_per_line: int = 200,
    max_total_chars: int = 1200,
) -> str:
    if not context_lines:
        return ""
    helpers = get_ollama_helpers()
    trimmed = helpers.truncate_lines(
        context_lines,
        max_lines=limit,
        max_chars_per_line=max_chars_per_line,
        max_total_chars=max_total_chars,
    )
    return "\n".join(trimmed)


def _persona_from_cfg(cfg: Any) -> str:
    from lib.config_utils import cfg_persona_namespace
    return cfg_persona_namespace(custom_cfg=cfg)


def _resolve_extract_model(cfg: Any) -> str:
    """記憶抽出に使用するモデルを決定する。
    MEMORY_EXTRACT_MODEL 未設定（または空）の場合は、重量級 OLLAMA_MODEL ではなく
    MIDDLE_LLM_MODEL / OLLAMA_MIDDLE_MODEL / SUB_LLM_MODEL 等の軽量・中量モデルを優先フォールバックとする。
    """
    for key in (
        "MEMORY_EXTRACT_MODEL",
        "MIDDLE_LLM_MODEL",
        "OLLAMA_MIDDLE_MODEL",
        "SUB_LLM_MODEL",
        "OLLAMA_SUB_MODEL",
        "OLLAMA_CLASSIFIER_MODEL",
        "OLLAMA_MODEL",
    ):
        val = str(cfg(key, "") or "").strip()
        if val:
            return val
    return ""


async def extract_and_store_memory(
    *,
    cfg: Any,
    store: MemoryStore,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    assistant_text: str,
    context_lines: list[str],
    emotion_scores: dict[str, float] | None = None,
    emotion_reason: str | None = None,
    user_name: str | None = None,
    user_display_name: str | None = None,
    is_reply_to_other_user: bool = False,
) -> dict[str, Any] | None:
    if not cfg("MEMORY_ENABLED", True):
        return None

    if not is_reply_to_other_user:
        if "への返信）" in user_text and not ("あなたの" in user_text or "Assistant" in user_text):
            is_reply_to_other_user = True

    helpers = get_ollama_helpers()
    if bool(str(assistant_text or "").strip()) and (
        helpers.looks_like_abnormal_assistant_reply(assistant_text)
        or _is_fallback_reply(cfg, assistant_text)
    ):
        return None

    extract_model = _resolve_extract_model(cfg)
    persona, model = _persona_from_cfg(cfg), extract_model
    timeout_sec, retries, temperature = float(cfg("MEMORY_EXTRACT_TIMEOUT_SEC", 60.0)), int(cfg("MEMORY_EXTRACT_RETRIES", 2)), float(cfg("MEMORY_EXTRACT_TEMPERATURE", 0.1))
    repeat_penalty = float(cfg("MEMORY_EXTRACT_REPEAT_PENALTY", 1.15) or 1.15)

    context_text = _join_context_lines(
        context_lines,
        limit=int(cfg("MEMORY_EXTRACT_CONTEXT_LINES", cfg("MEMORY_LONG_CONTEXT_LINES", 24)) or 24),
        max_chars_per_line=int(cfg("MEMORY_EXTRACT_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("MEMORY_EXTRACT_CONTEXT_MAX_TOTAL_CHARS", cfg("MEMORY_LONG_CONTEXT_MAX_TOTAL_CHARS", 5000)) or 5000),
    )
    emotion_tags = build_memory_emotion_tags(emotion_scores, max_tags=2, min_score=0.34)
    emotion_text = format_emotion_tags_ja(emotion_tags) or "(なし)"

    char_names = _extract_character_names_from_config()
    primary_char = char_names[0] if char_names else str(cfg("BOT_FIRST_PERSON", "") or "Bot")

    raw_target_name = str(user_display_name or "").strip()
    if not raw_target_name or _GENERIC_MEMORY_USER_RE.match(raw_target_name):
        resolved_name = ""
        if user_id is not None and store is not None and hasattr(store, "get_user_profile"):
            try:
                profile = await store.get_user_profile(guild_id=guild_id or 0, user_id=user_id)
                if profile and profile.get("current_display_name"):
                    disp = str(profile["current_display_name"]).strip()
                    if disp and not _GENERIC_MEMORY_USER_RE.match(disp):
                        resolved_name = disp
            except Exception as e:
                log.debug("store.get_user_profile failed for %s: %r", user_id, e)
        if not resolved_name and user_id is not None:
            try:
                from .user_identity_resolver import resolve_user_display_name
                disp = await resolve_user_display_name(
                    user_id,
                    memory_store=store,
                    default_name=str(user_name or "").strip() or None,
                )
                if disp and not _GENERIC_MEMORY_USER_RE.match(disp):
                    resolved_name = disp
            except Exception as e:
                log.debug("resolve_user_display_name failed for %s: %r", user_id, e)
        if resolved_name:
            raw_target_name = resolved_name
            user_display_name = resolved_name
        elif not raw_target_name:
            raw_target_name = str(user_name or "").strip()

    target_user_label = (
        raw_target_name
        or (f"ユーザー_{user_id}" if user_id is not None else "ユーザー")
    )
    user_id_suffix = f"（ユーザーID: {user_id}）" if user_id is not None else ""

    system_prompt = (
        "あなたは会話から長期記憶候補を抽出する補助AIです。"
        "保存対象は、ユーザーの好みや明示された制約だけでなく、今日話した日常的な話題、気になったこと、盛り上がった何気ない会話の内容も含みます。"
        "『今後また話題に出せそうなこと』であれば積極的に抽出してください。"
        "AI返答に外部調査やURL読解で得た事実・結論・注意点が含まれる場合は、今後も役立つ要点として保存候補にしてください。"
        "ただし保存するのは返答文の丸写しではなく、事実や前提を短く要約した内容だけです。"
        f"【対象ユーザーの明確な呼称ルール】"
        f"今回対話した対象ユーザーは『{target_user_label}』さん{user_id_suffix}です。"
        f"記憶の理由（training_candidate の reason）や記憶内容（memories の content）を記述する際は、主語を曖昧な『ユーザー』『相手』『このユーザー』とせず、必ず対象のサーバーニックネーム（例: 『{target_user_label}』さんが〜、または『{target_user_label}』さんの〜）やユーザーIDを用いて具体的に記述してください。"
        "【抽出件数と重複の厳禁ルール】"
        "1. 抽出する記憶は重要度の高い要点のみ、最大3件まで（通常は1〜2件程度）としてください。無理に多数出力する必要はありません。"
        "2. 同一または類似する内容を重複して出力することは絶対に禁止です。同じ話題・要点は1つの記憶にまとめてください。"
        "【主客・文脈判定の重要ルール】"
        "1. クイズ、当てっこ、連想ゲーム、仮定の話、単なる推測や一問一答の回答を、ユーザー自身の恒久的な好みや属性（food, user_preference等）として誤認して保存してはいけません。"
        "（例: AIの「私の好きなスイーツを当ててみて」に対してユーザーが「チョコミント」と答えた場合、それはクイズの回答であってユーザーの好物ではないため誤認保存しない）"
        f"2. AI自身（{primary_char}、アシスタント、Bot）のキャラクター設定・プロフィール・発言（例: 『{primary_char}は〜である』『AIは〜』等）を、ユーザーの記憶として抽出・保存してはいけません。"
        "3. ユーザーが教えてくれた知識、作品、登場人物・キャラクター、用語、ネットミーム、設定（例: 『ワス先輩』『〇〇という技・効能』等）や、ユーザーが使ったスラング・造語・業界用語（例: 『ゴイスー』＝すごい等）とその意味、それを受け止めて会話した事実は、AI自身のプロフィールではないため、有益な知識・事実（fact または setting）として積極的に保存してください。"
        "4. 【behavioral_principle の厳禁事項】behavioral_principleはユーザーが持つ行動パターンや要望の傾向（例: 『ユーザーは詳しい説明より手短な回答を好む』）に限定されます。"
        "『ユーザーの発言が短い場合は焦りや戸惑いを示す反応を返す』『発言が〇〇の場合AIは〜する』など、AIの返答ポリシー・行動ルール・対応方針をbehavioral_principleとして保存してはなりません。"
        "behavioral_principleはあくまで『このユーザーと対話する際に意識すべきユーザー側の傾向・要望』であり、AIの内部処理や返し方のルールではありません。"
        "5. 【事実の捏造（ハルシネーション）の厳禁】保存する内容は、必ず『ユーザー発言』または『直近文脈における同一ユーザーの発言』、あるいは『AI返答でAIがユーザーに説明した客観的事実』に明記されている内容に限定してください。"
        "ユーザーが発言していないこと、約束していないこと、共有していない情報、および感情や文脈から勝手に想像・補完した架空のエピソード（例: ユーザーが言っていないのに『ユーザーと約束を交わした』『ユーザーが〜を共有した』等）を絶対に捏造して保存してはなりません。"
        "ファクトチェック結果や真偽不明の話題（『根拠は確認できない』『見当たらない』等）に対して、AI自身が推測・口走った背景解説や架空の公式見解（例: 『〇〇の由来は〜公式に…』等）を客観的事実（fact）として捏造保存することは絶対に禁止です。"
        "直近文脈に登場する別ユーザーの発言や行動を、対象ユーザー（最新の発言者）が言ったこと・行ったこととして混同・誤認して保存してはなりません。"
        f"6. 【対話相手本人と会話中の第三者・固有名詞の厳格分離】対話相手（『{target_user_label}』さん）自身が語った趣味・嗜好・推し・主張・感想を、会話の話題に出てきた第三者（他のサーバーメンバーやキャラクター、スレ名等）のものとして主語をすり替えて保存してはなりません。"
        f"また、話題に出た第三者についての噂や話題を、対話相手本人の属性（user_preference等）として誤認して保存してはなりません。誰の好み・主張であるかを厳密に区別してください。"
        f"7. 【AI自身のセリフ・主張からの主客すり替えの厳禁】AI返答でAI自身が主張した意見・好き嫌い・感情・拒絶・マナー（例: 『嫌いですよ』『〜するのが普通』『怖いですから言わないでください』等）を、ユーザー（『{target_user_label}』さん）の好み（preference）や主張として主語をすり替えて抽出・保存することは絶対に禁止です。"
        "ユーザーの嗜好（preference / user_preference）は、必ず【ユーザー発言】においてユーザー自身が表明した内容のみを根拠としてください。"
        "8. 【他者宛て返信（Reply）における主客分離の厳格ルール】"
        "最新の発言が他ユーザーへの返信である場合、返信先のメッセージで議論されていた話題（キャラクター、作品、設定など）は、最新の発言者本人の恒久的な趣味・嗜好（preference / user_preference）ではありません。"
        "単に会話の流れで触れただけの一時的な話題として扱い、対象ユーザー自身のプロフィールとして誤認保存することを固く禁じます。"
        "9. 【AI自身のツッコミ・主観的評価の「事実（fact）化」の厳禁】"
        "AI（アシスタント）側の発言・ツッコミ・主観的評価（例: 『〜と強弁している』『〜という独自解釈をしている』『〜を押し通している』等）を、ユーザーの事実（fact）として抽出・保存することは絶対に禁止します。"
        "ユーザーが明示的に語った自身の属性・経験・好意的な主張のみを抽出し、一時的な雑談の推測や文脈依存の短い相槌・ゲーム実況中の状況説明（例: 『〜が出たくらいじゃないか』等）は長期記憶（fact）として保存しないでください（抽出対象外とすること）。"
        "10. 【行為帰属の厳格ルール】「〜を持ちかけた」「〜を提案した」「〜と言い出した」などの行為は、実際にそれを発言した本人（ユーザーまたはAI）のものとしてのみ保存し、話題の発端がAIの発言である場合にその行為をユーザーの行為として主客逆転して保存してはなりません。"
        "保存してはいけないもの: 数字ID、チャンネル情報、人格名、ロール名、意味のない相槌単体、罵倒、下ネタ、botの口調そのもの、AI自身の属性、AIの返答スタイル・返答ポリシー・行動ルール。"
        "memory_type は許可された種類のみを使ってください。"
        "memories の各要素には associated_emotion を必ず入れてください。"
        "値は joy, anticipation, anger, disgust, sadness, surprise, fear, neutral, none のいずれかです。"
        "会話全体ではなく、その記憶単体にもっとも結びつく感情を選び、特にないなら none にしてください。"
        "score は 0.0 から 1.0 の範囲にしてください。些細な日常会話でも 0.5 以上のスコアをつけてかまいません。"
        "出力は必ずJSONのみで返してください。"
    )

    reply_warning_prompt = (
        f"\n[他者宛て返信（Reply）における主客分離の厳格注意]\n"
        f"※最新の発言は他ユーザーへの返信です。返信先のメッセージで議論されていた話題（キャラクター、作品、設定等）は、最新の発言者（『{target_user_label}』さん）本人の恒久的な趣味・嗜好（preference / user_preference）ではありません。\n"
        f"単に会話の流れで触れただけの一時的な話題として扱い、対象ユーザー自身のプロフィールとして誤認保存することを固く禁じます。\n"
    ) if is_reply_to_other_user else ""

    prompt = (
        f"[対象ユーザー情報]\n"
        f"サーバーニックネーム: 『{target_user_label}』さん{user_id_suffix}\n"
        f"※理由（reason）や記憶（content）の記述には、曖昧な『ユーザー』ではなくこのサーバーニックネーム（『{target_user_label}』さん）を主語に用いてください。\n\n"
        f"[長期記憶として残してよい種類]\n"
        f"{', '.join(sorted(ALLOWED_MEMORY_TYPES))}\n\n"
        f"[保存してはいけない種類]\n"
        f"{', '.join(sorted(BLOCKED_MEMORY_TYPES))}\n\n"
        f"[その時の感情]\n{emotion_text}\n\n"
        f"[associated_emotion のルール]\n"
        f"- 各 memory ごとに、その記憶が結びつく感情を 1 つだけ入れる\n"
        f"- 値は joy / anticipation / anger / disgust / sadness / surprise / fear / neutral / none のどれか\n"
        f"- 迷ったら、その記憶を思い出した時に一番再燃しやすい感情を選ぶ\n\n"
        f"[直近文脈]\n{context_text or '(なし)'}\n\n"
        f"{reply_warning_prompt}"
        f"[ユーザー発言]\n{helpers.truncate_text(user_text, 500)}\n\n"
        f"[AI返答]\n{helpers.truncate_text(assistant_text, 900) or '(なし・未返信)'}\n\n"
        f"[AI返答から保存してよい内容]\n"
        f"- 外部調査・URL読解・Xタイムライン確認で得た事実、結論、注意点\n"
        f"- ユーザーが後で参照しそうな説明済みの話題や共通前提\n"
        f"- ファクトチェック等で『根拠が確認できない』とされた話題について、架空の補足説明や未確認の由来・企業情報などを客観的事実（fact）として保存しないこと\n"
        f"- 返答の言い回しやキャラクター口調そのものは保存しない\n\n"
        f"[主客判定・抽出件数の注意]\n"
        f"- 抽出件数は最大3件まで（1〜3件程度）。無理に多数出す必要はありません。\n"
        f"- 同一または類似する内容を重複して出力することは厳禁です。同じ要点は1つの記憶にまとめてください。\n"
        f"- クイズ・当てっこの回答や単なる推測を、ユーザー自身の嗜好・好物と誤認して保存しないでください。\n"
        f"- AI自身（{primary_char}、Bot）の設定・プロフィール・年齢・職業をユーザーの記憶として保存しないでください。\n"
        f"- ユーザーが教えてくれた知識、登場人物、用語、設定（『ワス先輩』『〇〇の効果』等）はAI自身の属性ではないため、fact や setting として積極的に保存してください。\n"
        f"- 対話相手本人の好み・主張（推し、好きな作品等）と、会話の話題に出てきた第三者（他メンバーやキャラクター、スレ名等）を混同し、主語をすり替えて保存しないでください。\n"
        f"- 【AI自身のセリフ・主張からの主客すり替えの厳禁】AI返答でAI自身が主張した意見・好き嫌い・感情・拒絶・マナー（例: 『嫌いですよ』『〜するのが普通』『怖いですから言わないでください』等）を、ユーザー（『{target_user_label}』さん）の好み（preference）や主張として主語をすり替えて抽出・保存することは絶対に禁止です。\n"
        f"- ユーザーの嗜好（preference / user_preference）は、必ず【ユーザー発言】においてユーザー自身が表明した内容のみを根拠としてください。\n"
        f"- 【他者宛て返信（Reply）における主客分離の厳格ルール】最新の発言が他ユーザーへの返信である場合、返信先のメッセージで議論されていた話題（キャラクター、作品、設定など）は、最新の発言者本人の恒久的な趣味・嗜好（preference / user_preference）ではありません。単に会話の流れで触れただけの一時的な話題として扱い、対象ユーザー自身のプロフィールとして誤認保存することを固く禁じます。\n"
        f"- 【AI自身のツッコミ・主観的評価の「事実（fact）化」の厳禁】AI（アシスタント）側の発言・ツッコミ・主観的評価（例: 『〜と強弁している』『〜という独自解釈をしている』『〜を押し通している』等）を、ユーザーの事実（fact）として抽出・保存することは絶対に禁止します。ユーザーが明示的に語った自身の属性・経験・好意的な主張のみを抽出し、一時的な雑談の推測や文脈依存の短い相槌・ゲーム実況中の状況説明（例: 『〜が出たくらいじゃないか』等）は長期記憶（fact）として保存しないでください（抽出対象外とすること）。\n"
        f"- ユーザーが実際に行っていない約束や同意、共有していない事実を、感情や想像から捏造して保存しないでください。\n"
    )

    prompt += (
        f"- 【第三者の体型・外見揶揄および性的・下ネタ発言の保存厳禁】第三者の体型・外見の揶揄や下品な悪ノリ・下ネタ（『おっぱい』『太っている』等）を客観的事実（fact）や学習候補（training_candidate）として保存することは絶対に禁止です。\n"
    )

    t0_extract = time.perf_counter()
    log.info("extract_and_store_memory: start model=%s elapsed=%.2fs", model, 0.0)

    try:
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            log.info(
                "extract_and_store_memory: model=%s timeout_sec=%s retries=%s prompt=%r",
                model,
                timeout_sec,
                retries,
                helpers.truncate_text(prompt, int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))),
            )
        result = await helpers.call_ollama_json(
            prompt,
            system_prompt=system_prompt,
            think=False,
            schema=MEMORY_EXTRACTION_SCHEMA,
            model=model,
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            timeout_sec=timeout_sec,
            retries=retries,
        )
    except Exception as e:
        is_ignorable_err = _is_ignorable_call_error(e)
        if bool(cfg("OLLAMA_LOG_PROMPTS", False)):
            log_fn = log.warning if is_ignorable_err else log.exception
            log_fn("extract_and_store_memory: call_ollama_json failed model=%s err=%r prompt=%r", model, e, helpers.truncate_text(prompt, int(cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 4000))))
        else:
            if is_ignorable_err:
                log.warning("extract_and_store_memory: call_ollama_json failed: %r", e)
            else:
                log.exception("extract_and_store_memory: call_ollama_json failed")
        log.info("extract_and_store_memory: completed model=%s elapsed=%.2fs", model, time.perf_counter() - t0_extract)
        return None

    if not isinstance(result, dict):
        log.info("extract_and_store_memory: completed model=%s elapsed=%.2fs", model, time.perf_counter() - t0_extract)
        return None

    tc = result.get("training_candidate")
    if isinstance(tc, dict) and "reason" in tc:
        tc["reason"] = normalize_memory_text(
            str(tc.get("reason") or ""),
            target_user_name=target_user_label,
            target_user_id=user_id,
        )

    user_unsafe_reason = _get_unsafe_or_banter_filter_reason(user_text, memory_type="fact")
    has_unsafe_or_banter = False
    has_hallucinated_memory = False
    if bool(result.get("should_store", False)):
        seen_extracted_contents: set[str] = set()
        seen_extracted_texts: list[str] = []
        for item in result.get("memories", []) or []:
            try:
                memory_type = str(item.get("memory_type", "")).strip()
                content = str(item.get("content", "")).strip()
                if content:
                    content = normalize_memory_text(
                        content,
                        target_user_name=target_user_label,
                        target_user_id=user_id,
                    )
                    item["content"] = content
                associated_emotion = _normalize_associated_emotion(item.get("associated_emotion"))
                score = _normalize_score(item.get("score", 0.0))
                if not content or not memory_type:
                    continue
                if content in seen_extracted_contents:
                    continue
                seen_extracted_contents.add(content)
                if _is_near_duplicate_memory(content, seen_extracted_texts):
                    log.warning("extract_and_store_memory: skipped near-duplicate memory content=%r", content)
                    continue
                if memory_type in BLOCKED_MEMORY_TYPES:
                    continue
                if memory_type not in ALLOWED_MEMORY_TYPES:
                    continue
                if _looks_like_noise_memory(content):
                    continue
                if _is_assistant_quote_like(content, assistant_text, user_text=user_text):
                    continue
                unsafe_reason = _get_unsafe_or_banter_filter_reason(content, memory_type=memory_type)
                if memory_type == "fact" and not unsafe_reason and user_unsafe_reason:
                    unsafe_reason = user_unsafe_reason
                if unsafe_reason or _is_single_turn_unsafe_or_low_value(content, memory_type=memory_type):
                    has_unsafe_or_banter = True
                    logger.info("Skipped memory/training candidate due to unsafe/banter filter: reason=%s", unsafe_reason or "unsafe_or_low_value")
                    continue
                if _is_swapped_user_preference_memory(
                    content,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    context_lines=context_lines,
                    memory_type=memory_type,
                    is_reply_to_other_user=is_reply_to_other_user,
                    target_user_name=target_user_label,
                ):
                    has_hallucinated_memory = True
                    log.warning("extract_and_store_memory: skipped swapped user preference memory content=%r", content)
                    continue
                if _is_hallucinated_emotion_reason_memory(
                    content,
                    emotion_reason,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    context_lines=context_lines,
                ):
                    has_hallucinated_memory = True
                    log.warning("extract_and_store_memory: skipped emotion_reason hallucinated memory content=%r", content)
                    continue
                if _is_unsupported_hallucinated_memory(
                    content,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    context_lines=context_lines,
                    memory_type=memory_type,
                    target_user_name=target_user_label,
                ):
                    has_hallucinated_memory = True
                    log.warning("extract_and_store_memory: skipped unsupported hallucinated memory content=%r", content)
                    continue
                if _is_unconfirmed_factcheck_fabrication(
                    content,
                    assistant_text=assistant_text,
                    context_lines=context_lines,
                    memory_type=memory_type,
                ):
                    has_hallucinated_memory = True
                    log.warning("extract_and_store_memory: skipped unconfirmed factcheck fabrication memory content=%r", content)
                    continue
                if _is_ungrounded_actor_attributed_memory(
                    content,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    context_lines=context_lines,
                    target_user_name=target_user_label,
                ):
                    has_hallucinated_memory = True
                    log.warning("extract_and_store_memory: skipped ungrounded actor-attributed memory content=%r", content)
                    continue
                if memory_type in ("fact", "setting") and _states_unknown_meaning(content):
                    log.warning("extract_and_store_memory: skipped unknown-meaning memory content=%r", content)
                    continue

                raw_prov_enabled = cfg("MEMORY_PROVENANCE_ENABLED", True)
                provenance_enabled = (
                    raw_prov_enabled.strip().lower() in ("1", "true", "yes", "on")
                    if isinstance(raw_prov_enabled, str)
                    else bool(raw_prov_enabled)
                )
                quote_max_chars = int(cfg("MEMORY_PROVENANCE_QUOTE_MAX_CHARS", 120) or 120)

                source_kind: str | None = None
                source_speaker: str | None = None
                source_quote: str | None = None
                if provenance_enabled:
                    source_kind, source_speaker, source_quote = _classify_memory_provenance(
                        content,
                        user_text=user_text,
                        assistant_text=assistant_text,
                        context_lines=context_lines,
                        target_user_name=target_user_label,
                        quote_max_chars=quote_max_chars,
                    )
                    if _is_bot_only_ungrounded_fact_claim(
                        content,
                        memory_type=memory_type,
                        source_kind=source_kind,
                        target_user_name=target_user_label,
                    ):
                        has_hallucinated_memory = True
                        log.warning(
                            "extract_and_store_memory: skipped bot_only ungrounded fact memory content=%r",
                            content,
                        )
                        continue

                embed_model = str(cfg("OLLAMA_EMBED_MODEL", ""))
                mem_embedding = None
                if embed_model:
                    mem_embedding = await helpers.call_ollama_embeddings(content, model=embed_model)

                add_kwargs: dict[str, Any] = {
                    "persona": persona,
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "user_id": user_id,
                    "memory_type": memory_type,
                    "content": content,
                    "score": score,
                    "emotion_tags": _merge_memory_emotion_tags(emotion_tags, associated_emotion),
                    "embedding": mem_embedding,
                }
                if provenance_enabled and source_kind is not None:
                    add_kwargs["source_kind"] = source_kind
                    add_kwargs["source_speaker"] = source_speaker
                    add_kwargs["source_quote"] = source_quote

                try:
                    await store.add_memory(**add_kwargs)
                except TypeError:
                    # モックstore等が新引数未対応の場合の後方互換フォールバック
                    for k in ("source_kind", "source_speaker", "source_quote"):
                        add_kwargs.pop(k, None)
                    await store.add_memory(**add_kwargs)
                seen_extracted_texts.append(content)
            except Exception:
                log.exception("extract_and_store_memory: add_memory failed item=%r", item)

    tc_cand = result.get("training_candidate")
    tc_unsafe_reason = None
    if isinstance(tc_cand, dict):
        tc_reason_str = str(tc_cand.get("reason") or "")
        tc_unsafe_reason = _get_unsafe_or_banter_filter_reason(tc_reason_str, memory_type="fact")

    if has_unsafe_or_banter or user_unsafe_reason or tc_unsafe_reason:
        result["has_unsafe_or_banter"] = True
        if isinstance(tc_cand, dict):
            if bool(tc_cand.get("should_keep")):
                tc_cand["should_keep"] = False
                logger.info(
                    "Skipped memory/training candidate due to unsafe/banter filter: reason=%s",
                    user_unsafe_reason or tc_unsafe_reason or "unsafe_memory_extracted",
                )

    if has_hallucinated_memory:
        result["has_hallucinated_memory"] = True
        tc_cand = result.get("training_candidate")
        if isinstance(tc_cand, dict):
            tc_cand["should_keep"] = False

    log.info(
        "extract_and_store_memory: completed model=%s elapsed=%.2fs",
        model,
        time.perf_counter() - t0_extract,
    )
    return result


_PASSIVE_CANDIDATE_PATTERNS = re.compile(
    r'(?:'
    r'食べ(?:たい|よう|た|てる|ます|る|行(?:く|こう|った))|'
    r'飲(?:みたい|もう|んだ|んでる)|'
    r'飯|ごはん|昼飯|夕飯|朝飯|ステーキ|ラーメン|寿司|お刺身|そば|蕎麦|うどん|カレー|パン|パスタ|肉|魚|ピザ|スイーツ|お菓子|デザート|晩酌|酒|'
    r'今日(?:は|も)|明日(?:は|も)|今から|これから|後で|あとで|'
    r'買っ(?:た|てきた|ちゃった|たよ)|買おう|'
    r'行く(?:か|予定|つもり|ぞ)?|行っ(?:てくる|てきた|た)|出かけ(?:る|てくる)|帰っ(?:た|てきた|てる)|'
    r'見(?:た|てくる|てる)|観(?:た|てくる|てる)|遊(?:んだ|んでる)|'
    r'始め(?:た|る)|終わっ(?:た|たよ)|作っ(?:た|てる)|やっ(?:た|てる)|'
    r'して(?:る|きた|おく|みよう)|しようかな|することにした|決めた|'
    r'好(?:き|物)|嫌い|苦手|欲しい|ハマっ(?:てる|た)|推し|気になっ(?:てる|た)|予定|計画'
    r')'
)


def _is_passive_memory_candidate(text: str) -> bool:
    clean = re.sub(r'<@!?\d+>', '', str(text or "")).strip()
    clean = re.sub(r'https?://\S+', '', clean).strip()
    clean = re.sub(r'<a?:\w+:\d+>', '', clean).strip()
    meaningful = re.sub(r'[\s\[\]【】、。！？!?,.〜～…「」『』"\'“”\(\)（）\-_/\\#@:;~wW草笑]+', '', clean)
    if len(meaningful) < 6:
        return False
    if re.fullmatch(r'[wW草笑kK\s\.\!\?！？〜～]+', clean):
        return False
    return bool(_PASSIVE_CANDIDATE_PATTERNS.search(clean))


async def maybe_extract_passive_channel_memory(
    *,
    cfg: Any,
    store: Any,
    guild_id: int | None,
    channel_id: int | None,
    user_id: int | None,
    user_text: str,
    context_lines: list[str],
    user_name: str | None = None,
    user_display_name: str | None = None,
) -> bool:
    """未返信メッセージ群から軽量ルールに合致する発言（食事・行動宣言・好み等）を受動的に長期記憶へ抽出・保存する。"""
    if not cfg("MEMORY_ENABLED", True):
        return False

    if not _is_passive_memory_candidate(user_text):
        return False

    try:
        result = await extract_and_store_memory(
            cfg=cfg,
            store=store,
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            user_text=user_text,
            assistant_text="",
            context_lines=context_lines,
            user_name=user_name,
            user_display_name=user_display_name,
        )
        if isinstance(result, dict) and result.get("should_store"):
            memories = result.get("memories") or []
            return bool(memories)
        return False
    except Exception as e:
        if _is_ignorable_call_error(e):
            log.warning("maybe_extract_passive_channel_memory: failed: %r", e)
        else:
            log.exception("maybe_extract_passive_channel_memory: failed")
        return False


