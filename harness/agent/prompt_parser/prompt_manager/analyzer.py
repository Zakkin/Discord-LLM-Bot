from __future__ import annotations

import difflib
import logging
import re
from typing import Any

from ollama_bot.common.config_helpers import cfg
from ollama_bot.common.emotion_helpers import format_emotion_tags_ja
from ollama_bot.common import ollama_helpers
from ollama_bot.common.bot_identity import BotIdentity, is_bot_name_mentioned, resolve_bot_identity
from .analysis_prompts import (
    MCP_ROUTER_SYSTEM_PROMPT,
    MCP_ROUTING_DECISION_SCHEMA,
    PRE_REPLY_ANALYZER_SYSTEM_PROMPT,
    build_mcp_routing_prompt,
    build_pre_reply_analyzer_system_prompt,
)
from lib.text_utils import normalize_compare_text as _normalize_compare_text
from .budget import (
    _clean_analyzer_keyword,
    _clean_analyzer_note,
    _clean_selected_memory_indices,
    sanitize_memory_candidates,
)

log = logging.getLogger("harness.agent.prompt_manager.analyzer")

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
        "topic_origin": {"type": "string", "maxLength": 60},
    },
    "required": [
        "intent", "target_is_ai", "should_clarify", "selected_indices", "emotion_focus",
        "reply_strategy", "avoid", "persona_risk", "consistency_correction",
        "is_breakdown", "breakdown_reason", "uncertainty_level", "needs_clarification",
        "topic_origin",
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

    log.info(
        "[prompt-manager] Third-party hearsay detected: subject=%r, content=%r",
        subject,
        content_str[:50],
    )
    return True, subject, content_str


def looks_like_ai_utterance_confirmation(
    user_text: str,
    *,
    last_assistant_text: str = "",
) -> bool:
    """ユーザーの最新発言が、直前のAI発言に対する短い確認・驚き（『マジすか』『本当？』『マジで？』等）であるかを判定する。"""
    if not (last_assistant_text and last_assistant_text.strip()):
        return False
    raw = str(user_text or "").strip()
    if not raw:
        return False
    clean = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw, flags=re.DOTALL).strip()
    clean = re.sub(r"^[（(].*?への返信[）)]\s*", "", clean, flags=re.DOTALL).strip()

    # 質問・相談等の構文（〜についてどう思う、〜を教えて等）は確認ではない
    if any(op in clean for op in ("どう思う", "どう考える", "どうですか", "教えて", "教えろ")):
        return False

    # 記号、顔文字、絵文字等を除去し、日本語文字および英数字のみ抽出
    compact = re.sub(r"[^\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FA5a-zA-Z0-9]", "", clean)
    # 末尾の草・笑い(w, 草, 笑)を除去
    compact = re.sub(r"[wW草笑]+$", "", compact)
    # 長音符(ー)を除去
    compact = re.sub(r"[ー〜~]+$", "", compact)
    lowered = compact.lower()

    if not lowered or len(lowered) > 20:
        return False

    confirmation_exact = {
        "マジすか", "まじすか", "マジですか", "まじですか", "マジで", "まじで", "マジ", "まじ",
        "マジっすか", "まじっすか", "マジか", "まじか", "マジかい", "まじかい",
        "ガチで", "がちで", "ガチですか", "がちですか", "ガチすか", "がちすか",
        "本当ですか", "ほんとですか", "本当", "ほんと", "本当に", "ほんとに", "本当なの", "ほんとなの", "本当か", "ほんとか", "本当かい", "ほんとかい",
        "嘘でしょ", "うそでしょ", "ウソでしょ", "嘘だろ", "うそだろ", "ウソだろ", "嘘", "うそ", "ウソ", "嘘やん", "うそやん",
        "知ってるの", "知ってるんですか", "知ってたの", "知ってたんですか", "知ってんの",
        "好きだったの", "好きだったんですか", "好きなの", "好きなんですか",
        "食べたの", "食べたんですか", "行ったの", "行ったんですか",
        "食べたことあるの", "食べたことあるんですか", "行ったことあるの", "行ったことあるんですか",
    }
    if lowered in confirmation_exact:
        return True

    confirmation_patterns = (
        r"^(?:マジ|まじ|ガチ|がち)(?:すか|っすか|ですか|でっか|なんですか|なの|なん|で|に|か|かい)?$",
        r"^(?:本当|ほんと)(?:すか|ですか|でっか|なんですか|なの|なん|で|に|か|かい)?$",
        r"^(?:嘘|うそ|ウソ)(?:でしょ|だろ|つけ|やん|乙)?$",
        r"^(?:知っ|知て)(?:てるの|てるんですか|てたの|てたんですか|てんの)?$",
        r"^(?:好き|すき)(?:なの|なんですか|だったの|だったんですか)?$",
        r"^(?:行っ|たべ|食べ)(?:たの|たんですか|たことあるの|たことあるんですか)?$",
    )
    return any(bool(re.match(p, lowered)) for p in confirmation_patterns)


def looks_like_user_deduction_or_confirmation(
    user_text: str,
    *,
    last_assistant_text: str = "",
) -> bool:
    """ユーザーの最新発言が、直前のAI発言・説明から導かれた自然な帰結・要約・確認（『じゃあ〇〇なんだ』『つまり〇〇か』等）かを判定する。"""
    if not (last_assistant_text and last_assistant_text.strip()):
        return False
    raw = str(user_text or "").strip()
    if not raw:
        return False
    clean = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw, flags=re.DOTALL).strip()
    clean = re.sub(r"^[（(].*?への返信[）)]\s*", "", clean, flags=re.DOTALL).strip()
    if not clean or len(clean) > 50:
        return False
    compact = re.sub(r"\s+", "", clean)

    # 1. 「じゃあ」「では」「なら」「つまり」「ということは」「ってことは」「要するに」等の帰結接続詞で始まるパターン
    has_deduction_prefix = bool(re.match(
        r"^(?:じゃあ|では|じゃ|なら|つまり|ということは|と言うことは|てことは|ってことは|要するに)",
        compact,
    ))
    if has_deduction_prefix:
        # 明確な命令・要求・操作指示（〜教えて、〜してくれ、〜調べて 等）は除外
        if re.search(r"(?:教えて|教えろ|してくれ|やってくれ|してよ|どう思う|どうなる|調べて|要約して)", compact):
            return False
        # 通常の要約・確認・納得文（「〜なんだ」「〜だね」「〜か」「〜ないんだ」「〜ないね」等）
        return True

    # 2. 文末が「〜ってことね」「〜ってことか」「〜ということか」「〜というわけか」等の要約・確認パターン
    if re.search(
        r"(?:ってことね|ってことか|ってことだね|ってことだな|ということね|ということか|というわけね|というわけか|って訳ね|って訳か)[!！?？〜~]*$",
        compact,
    ):
        return True

    # 3. 直前発言に対する短い安堵・確認（「雨降らないんだ」「雨は降らないね」「傘いらないんだ」等）
    if re.search(r"(?:雨(?:は|も)?(?:降らない|ふらない)|傘(?:は|も)?(?:いらない|不要)|濡れない)", compact) and re.search(r"(?:晴れ|降水|雨|天気|傘)", last_assistant_text):
        return True

    return False


_KANJI_NUM_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _parse_playful_count(val: str) -> int | None:
    if not val:
        return None
    val = val.strip()
    if val.isdigit():
        return int(val)
    if val in _KANJI_NUM_MAP:
        return _KANJI_NUM_MAP[val]
    return None


def extract_playful_say_request(text: str) -> tuple[str, int | None] | None:
    """ユーザーからの台詞指定・言葉遊び（〇〇って10回言って、好きって言って等）から対象単語と回数を抽出する。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    clean = re.sub(r"<@!?\d+>", "", raw).strip()
    clean = re.sub(r"^[,\s、。!！?？]+", "", clean)

    # 1. 回数指定パターン: (単語)って/と/を N回言って/言ってみて/言うて
    # 例: ピザって10回言って / 「膝」って十回言って / ピザと10回言ってみて / 膝を10回言ってほしい
    m_count = re.search(
        r"(?:^|[\s、,])(?:「([^」]+)」|『([^』]+)』|\"([^\"]+)\"|([^\s、,「『\"\'\n]+?))\s*(?:って|と|を)?\s*([0-9０-９]+|[一二三四五六七八九十]+)\s*回\s*(?:言って(?!た|いた|こと|んだ|んです)|言ってみて|言うて|唱えて(?!た|いた)|叫んで(?!た|いた))",
        clean,
    )
    if m_count:
        word = m_count.group(1) or m_count.group(2) or m_count.group(3) or m_count.group(4)
        c_str = m_count.group(5)
        c_str = c_str.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
        count = _parse_playful_count(c_str)
        if word:
            word = re.sub(r"(?:って|と|を)$", "", word).strip()
            if 1 <= len(word) <= 30:
                return (word, count)

    # 2. 単純なセリフ指定パターン: (単語)って/と言って/言ってみて/言ってほしい
    m_say = re.search(
        r"(?:^|[\s、,])(?:「([^」]+)」|『([^』]+)』|\"([^\"]+)\"|([^\s、,「『\"\'\n]+?))\s*(?:って|と)\s*(?:言って(?!た|いた|こと|んだ|んです)|言ってみて|言うて|呼んで(?!た|いた))(?:ほしい|欲しい|頼む|お願い|おねがい|くれ|よ|ください|ちょうだい|！|!|？|\?|$)",
        clean,
    )
    if m_say:
        word = m_say.group(1) or m_say.group(2) or m_say.group(3) or m_say.group(4)
        if word:
            word = re.sub(r"(?:って|と)$", "", word).strip()
            if 1 <= len(word) <= 30:
                return (word, None)

    return None


def looks_like_user_playful_request_or_command(text: str) -> bool:
    """ユーザーからの台詞指定・言葉遊び（10回言って等）・アクションのリクエストかを判定する。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if extract_playful_say_request(raw) is not None:
        return True
    patterns = [
        r"(?:って|と|を)?\s*(?:[0-9０-９]+|[一二三四五六七八九十]+)\s*回\s*(?:言って(?!た|いた|こと|んだ|んです)|言ってみて|言うて|唱えて(?!た|いた)|叫んで(?!た|いた))",
        r"(?:って|と)\s*(?:言って(?!た|いた|こと|んだ|んです)|言ってみて|言うて|呼んで(?!た|いた))(?:\s*|$|[!！?？よくれくださいちょうだい]|ほしい|欲しい|頼む|お願い|おねがい)",
        r"(?:を|で)?\s*(?:歌って(?!た|いた)|真似して(?!た|いた)|演じて(?!た|いた)|やってみて|してみて|唱えて(?!た|いた))(?:\s*|$|[!！?？よくれくださいちょうだい]|ほしい|欲しい|頼む|お願い|おねがい)",
        r"(?:言って(?!た|いた|こと|んだ|んです)|言ってみて|呼んで(?!た|いた)|歌って(?!た|いた)|真似して(?!た|いた)|演じて(?!た|いた))(?:くれ|よ|ください|ちょうだい|ほしい|欲しい|頼む|お願い|おねがい|！|!|？|\?|$)",
    ]
    return any(re.search(p, raw) for p in patterns)


def looks_like_user_reply_request(text: str) -> bool:
    """メッセージがAI宛て返信等における依頼・要求表現（お願いします、〜して、〜作って、〜詠んで、〜教えて、どう思う等）を含むか判定する。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    clean = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw, flags=re.DOTALL).strip()
    if not clean:
        return False
    if looks_like_user_playful_request_or_command(clean):
        return True

    request_keywords = (
        "お願いします", "お願い", "おねがい", "頼む", "たのむ", "頼みます",
        "どう思う", "どうおもう", "どう考える", "どう思う？", "どうですか",
        "教えて", "おしえて", "聞かせて", "きかせて",
    )
    if any(kw in clean for kw in request_keywords):
        return True

    request_patterns = [
        r"(?:作|つく|詠|よ|書|か|描|歌|うた|話|はな|語|かた|送|おく|考|かんがえ|教|おし)って?(?:くれ|ください|ちょうだい|ほしい|欲しい|もらえ|頼む|お願い|おねがい|みて|頂戴|たまえ)",
        r"(?:して|やって)(?:くれ|ください|ちょうだい|ほしい|欲しい|もらえ|頼む|お願い|おねがい|みて|頂戴|たまえ)",
        r"(?:作|つく|詠|よ|書|か|描|歌|うた|演じ|真似し|やって|して)って?(?:よ|ね|！|!|？|\?|$)",
        r"(?:一句|1句|短歌|川柳|詩|曲|歌|コード|案|意見|感想)(?:を|で)?(?:お願いします|お願い|おねがい|詠んで|作って|考えて|聞かせて)",
    ]
    return any(re.search(p, clean) for p in request_patterns)


def is_reply_to_bot_message(
    user_text: str,
    *,
    is_reply_to_bot: bool = False,
    bot_identity: BotIdentity | None = None,
) -> bool:
    """メッセージがAI宛ての返信（Reply）であるかを判定する。"""
    if is_reply_to_bot:
        return True
    raw = str(user_text or "").strip()
    if not raw:
        return False
    if re.search(r"^[（(](?:あなた|assistant|Assistant)(?:の「.+?」)?への返信[）)]", raw, flags=re.IGNORECASE):
        return True
    reply_match = re.search(
        r"^[（(]([^\s（）()「」\n]{1,40}?)(?:の「.+?」)?への返信[）)]",
        raw,
        flags=re.DOTALL,
    )
    if reply_match:
        target = reply_match.group(1).strip()
        clean_target = re.sub(r"^(?:相手|ユーザー)\((.*?)\)$", r"\1", target).strip()
        bot_aliases = _get_non_third_party_subjects()
        if (
            clean_target.lower() in ("あなた", "assistant")
            or clean_target.lower() in bot_aliases
            or is_bot_name_mentioned(clean_target, bot_identity)
        ):
            return True
    return False


def _should_skip_intent_analysis(*, current_user_text: str, last_assistant_text: str) -> bool:
    text = (current_user_text or "").strip()
    if not text:
        return True

    if looks_like_user_playful_request_or_command(text) or looks_like_user_reply_request(text):
        return False

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
    is_reply_to_bot: bool = False,
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

    # AI宛て返信の早期判定
    is_reply_to_bot_detected = is_reply_to_bot_message(
        text,
        is_reply_to_bot=is_reply_to_bot,
        bot_identity=bot_identity,
    )
    if is_reply_to_bot_detected:
        is_reply_to_other_user = False

    is_playful_request = looks_like_user_playful_request_or_command(text)
    is_reply_request = looks_like_user_reply_request(text)

    if is_reply_to_bot_detected and (is_playful_request or is_reply_request):
        return {
            "intent": "依頼",
            "target_is_ai": True,
            "should_clarify": False,
            "reason": "heuristic_reply_to_bot_request",
        }

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

    direct_request = is_playful_request or is_reply_request or bool(re.search(
        r"(言って(?!た|いた|こと|んだ|んです)|言ってみて|言うて|呼んで(?!た|いた)|歌って(?!た|いた)|唱えて(?!た|いた)|叫んで(?!た|いた)|真似して(?!た|いた)|演じて(?!た|いた)|やって(?!た|いた)|やってみて|してみて|してくれ|してよ|しておくれ|してちょうだい|して\?|して？|して$|して。|して！|して!|教えて|教えろ|見せて|見せてくれ|答えて|話して|要約して|調べて|解説して|どうする|どう思う|できる\?|できる？|できるか|くれる\?|くれる？|ほしい|頼む|お願い)",
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

    bot_name_in_text = is_bot_name_mentioned(text, bot_identity) or bool(re.search(r"<@!?\d+>", text))

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

    address_or_manner_tsukkomi = bool(
        last_assistant_text
        and re.search(
            r"(呼び捨て|タメ口|ため口|タメぐち|敬語|さん付け|ちゃん付け|君付け|くん付け|呼び方|急にタメ|誰それ)",
            compact,
        )
    )

    if address_or_manner_tsukkomi:
        return {
            "intent": "ツッコミ",
            "target_is_ai": True,
            "should_clarify": False,
            "reason": "heuristic_address_or_manner_tsukkomi",
        }

    if looks_like_ai_utterance_confirmation(text, last_assistant_text=last_assistant_text):
        return {
            "intent": "確認",
            "target_is_ai": True,
            "should_clarify": False,
            "reason": "heuristic_ai_utterance_confirmation",
        }

    if looks_like_user_deduction_or_confirmation(text, last_assistant_text=last_assistant_text):
        return {
            "intent": "確認",
            "target_is_ai": False,
            "should_clarify": False,
            "reason": "heuristic_user_deduction_or_confirmation",
        }

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
    *,
    mentioned_users: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Middle LLMが選定した記憶から、教示文やメンバー言及時、無関係な全選択ハルシネーション等の記憶を除外する。"""
    if not selected_memories:
        return []

    from harness.agent.tool_registry.web_research.helpers import looks_like_user_teaching_or_definition

    is_teaching = looks_like_user_teaching_or_definition(user_text)
    has_mentioned_users = bool(mentioned_users)

    # 1. 教示文またはサーバーメンバー言及時：
    # ユーザーは特定の人や知識について話しているため、直接キーワードが一致しない過去の外部話題は除外する
    if is_teaching or has_mentioned_users:
        clean_user = _EXTERNAL_URL_RE.sub("", user_text or "").strip()
        matched = _filter_memories_matching_user_text(selected_memories, clean_user, last_assistant_text)
        if not matched:
            log.info(
                "[prompt-manager] Filtered out irrelevant memories during teaching/member mention: %s",
                selected_memories,
            )
        return matched

    # 2. 質問・意見求め（「〜どう思う」「〜とは」「〜って何」等）での全選択ハルシネーション対策：
    # 複数記憶が選ばれているが、ユーザー発言のキーワードがどの記憶にも一切含まれていない場合、
    # 直前の別話題（ワス等）の引きずり汚染を防ぐため安全にフィルタする
    is_inquiry = any(marker in user_text for marker in ("どう思う", "って何", "とは", "知ってる", "誰", "どこ"))
    if is_inquiry and len(selected_memories) >= 2:
        clean_user = _EXTERNAL_URL_RE.sub("", user_text or "").strip()
        matched = _filter_memories_matching_user_text(selected_memories, clean_user, last_assistant_text)
        if not matched:
            # 1つも一致しない場合は、LLMが直前話題の記憶を丸ごと引きずった可能性が高い
            log.info(
                "[prompt-manager] Filtered out ungrounded memory carryover during inquiry: %s",
                selected_memories,
            )
            return []
        selected_memories = matched

    # 3. 質問・意見照会におけるメタ記憶（「意味や文脈は不明」「質問した」等）の除外：
    # 実態記憶（エピソード・出来事）が共存している場合、メタ記憶を除外して実態記憶のみを残す
    from harness.agent.tool_registry.web_research.topic_investigation import is_meta_or_confused_memory
    if is_inquiry or has_mentioned_users:
        substantive = [m for m in selected_memories if not is_meta_or_confused_memory(m)]
        if substantive:
            return sanitize_memory_candidates(substantive, last_assistant_text=last_assistant_text)

    # 通常の雑談では、LLMの意味的な連想（パチンコ ⇔ 賭け話等）を尊重する
    return sanitize_memory_candidates(selected_memories, last_assistant_text=last_assistant_text)


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
    sanitized_memories = sanitize_memory_candidates(memory_lines, last_assistant_text=last_assistant_text)
    trimmed_memory_lines = ollama_helpers.truncate_lines(
        sanitized_memories,
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
    is_reply_to_bot: bool = False,
    reply_context_hint: str | None = None,
    recent_bot_utterances: list[str] | None = None,
    bot_identity: BotIdentity | None = None,
    mentioned_users: list[dict[str, Any]] | None = None,
    current_user_name: str = "",
) -> tuple[dict[str, Any], list[str], str | None, dict[str, Any]]:
    # 候補メモリの事前サニタイズ（多重防御: 会話ログ行や直前AI発言の混入を遮断）
    memory_lines = sanitize_memory_candidates(memory_lines, last_assistant_text=last_assistant_text)
    if bot_identity is None:
        bot_identity = resolve_bot_identity()

    reply_to_other_target = ""
    reply_to_other_match = re.search(
        r"[（(]([^\s（）()「」\n]{1,40}?)(?:の「.+?」)?への返信[）)]",
        current_user_text or "",
        flags=re.DOTALL,
    )
    if reply_to_other_match:
        matched_target_user = reply_to_other_match.group(1).strip()
        matched_target_clean = re.sub(r"^(?:相手|ユーザー)\((.*?)\)$", r"\1", matched_target_user).strip()
        bot_aliases = _get_non_third_party_subjects()
        if (
            matched_target_clean.lower() not in ("あなた", "assistant")
            and matched_target_clean.lower() not in bot_aliases
            and not is_bot_name_mentioned(matched_target_clean, bot_identity)
        ):
            is_reply_to_other_user = True
            reply_to_other_target = matched_target_clean or matched_target_user

    detected_reply_to_bot = is_reply_to_bot_message(
        current_user_text,
        is_reply_to_bot=is_reply_to_bot,
        bot_identity=bot_identity,
    )
    if detected_reply_to_bot:
        is_reply_to_other_user = False
        reply_to_other_target = ""

    is_reply_request_to_bot = bool(
        detected_reply_to_bot and (
            looks_like_user_reply_request(current_user_text)
            or looks_like_user_playful_request_or_command(current_user_text)
        )
    )

    log.info(
        "[prompt-manager] Pre-reply context analysis started: text_len=%d, memory_candidates=%d, is_reply_to_other_user=%s (target=%s), is_reply_to_bot=%s, is_reply_request=%s",
        len(current_user_text),
        len(memory_lines),
        is_reply_to_other_user,
        reply_to_other_target,
        detected_reply_to_bot,
        is_reply_request_to_bot,
    )

    intent_info = _infer_user_intent_without_llm(
        current_user_text=current_user_text,
        last_assistant_text=last_assistant_text,
        is_reply_to_other_user=is_reply_to_other_user,
        is_reply_to_bot=detected_reply_to_bot,
        bot_identity=bot_identity,
    )
    selected_lines: list[str] = []
    initial_strategy = "外野ツッコミ" if (
        is_reply_to_other_user and any(k in current_user_text for k in ("は？", "は?", "え？", "え?", "草", "なんで", "何で", "どういう"))
    ) else ("要望受容・ツッコミ" if is_reply_request_to_bot else ("見守り" if is_reply_to_other_user else "direct_response"))
    initial_avoid = (
        "要望を受け止め、キャラクターらしくリアクションする"
        if is_reply_request_to_bot
        else ("" if detected_reply_to_bot else ("人間同士の会話を無理に自分宛てと受け取るのを避ける" if is_reply_to_other_user else ""))
    )
    deliberation_info: dict[str, Any] = {
        "emotion_focus": "",
        "reply_strategy": initial_strategy,
        "avoid": initial_avoid,
        "persona_risk": False,
        "consistency_correction": "",
        "is_breakdown": False,
        "breakdown_reason": "",
        "uncertainty_level": "low",
        "needs_clarification": False,
        "topic_origin": "",
    }

    is_ai_confirmation = bool(
        looks_like_ai_utterance_confirmation(current_user_text, last_assistant_text=last_assistant_text)
        or intent_info.get("reason") == "heuristic_ai_utterance_confirmation"
    )
    if is_ai_confirmation:
        intent_info["target_is_ai"] = True
        deliberation_info["reply_strategy"] = "肯定・スタンス維持"
        deliberation_info["avoid"] = "前言撤回や未体験者ぶる態度を避ける"

    is_user_deduction = bool(
        looks_like_user_deduction_or_confirmation(current_user_text, last_assistant_text=last_assistant_text)
        or intent_info.get("reason") == "heuristic_user_deduction_or_confirmation"
    )
    if is_user_deduction:
        if intent_info.get("intent") in ("", "statement", "unclear", "monologue"):
            intent_info["intent"] = "確認"
        deliberation_info["reply_strategy"] = "受容・肯定"
        deliberation_info["avoid"] = "不自然な反論や被害妄想を避け、まず肯定する"
        deliberation_info["is_breakdown"] = False
        deliberation_info["breakdown_reason"] = ""

    if is_reply_request_to_bot:
        intent_info["target_is_ai"] = True
        intent_info["intent"] = "依頼"
        deliberation_info["reply_strategy"] = "要望受容・ツッコミ"
        deliberation_info["avoid"] = "要望を受け止め、キャラクターらしくリアクションする"
        deliberation_info["needs_clarification"] = False
        deliberation_info["uncertainty_level"] = "low"

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
            "おはよ", "こんにちは", "こんばんは", "ただいま", "おやすみ", "ありがとう", "草", "w", "笑",
            "なんで", "ほんと", "マジ", "まじ", "了解", "りょ", "うける", "やば", "かわいい", "可愛い", "かわよ",
            "それな", "たすかる", "助かる", "えらい", "すごい", "すげ", "うけた", "なるほど", "たしかに",
            "確かに", "そうなんだ", "そうなん", "うん", "はい", "そうそう", "へえ", "ほーん",
        )
        if not any(token in text_for_check for token in ("?", "？", "http://", "https://")):
            if len(text_for_check) <= 15 and any(m in text_for_check for m in casual_markers):
                is_casual = True
            elif len(text_for_check) <= 5:
                is_casual = True

        log.info(
            "[prompt-manager] Pre-reply analysis skipped LLM call: skip_intent=%s, skip_memory=%s, is_casual=%s, external_research=%s",
            skip_intent,
            skip_memory,
            is_casual,
            external_research_request,
        )

        if is_casual:
            selected_lines = []
        elif external_research_request:
            selected_lines = _filter_memories_matching_user_text(analyzer_memory_lines, text_for_check)[:3]
        else:
            selected_lines = analyzer_memory_lines[:3]
        selected_lines = sanitize_memory_candidates(selected_lines, last_assistant_text=last_assistant_text)

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
    if is_reply_to_other_user:
        target_info = f"『{reply_to_other_target}』さん" if reply_to_other_target else "別ユーザー"
        prompt_parts.append(
            f"【人間同士の会話（他者宛て返信）】最新の発言は、別ユーザー{target_info}への返信です。AI宛てではありません。"
            "返信先の発言はAIのものではなく人間の発言です。第三者・外野の視点（外野ツッコミ・見守り・仲裁等）で分析してください。"
        )
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
    if is_reply_to_other_user and reply_to_other_target:
        user_display = (
            f"最新のユーザー発言（対話相手: 『{current_user_name.strip()}』さん、返信先: 『{reply_to_other_target}』さん）: {trimmed_user_text}"
            if current_user_name and current_user_name.strip()
            else f"最新のユーザー発言（返信先: 『{reply_to_other_target}』さん）: {trimmed_user_text}"
        )
    else:
        user_display = (
            f"最新のユーザー発言（対話相手: 『{current_user_name.strip()}』さん）: {trimmed_user_text}"
            if current_user_name and current_user_name.strip()
            else f"最新のユーザー発言: {trimmed_user_text}"
        )
    prompt_parts.extend([
        user_display,
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
        log.info(
            "[prompt-manager] Invoking pre-reply analyzer LLM (model=%s, prompt_parts=%d)",
            model_name,
            len(prompt_parts),
        )
        result = await ollama_helpers.call_ollama_json(
            "\n".join(prompt_parts),
            system_prompt=system_prompt,
            schema=_PRE_REPLY_ANALYZER_SCHEMA,
            think=False,
            model=model_name,
            retries=max(int(cfg("OLLAMA_INTENT_ANALYZER_RETRIES", 1) or 1), 0),
            temperature=0.0,
            num_predict=max(int(cfg("OLLAMA_INTENT_ANALYZER_NUM_PREDICT", 640) or 640), 384),
            num_ctx=max(int(cfg("OLLAMA_MIDDLE_NUM_CTX", 8192) or 8192), 8192),
        )
        log.info(
            "[prompt-manager] Pre-reply analyzer LLM returned: raw_intent=%s, target_is_ai=%s, should_clarify=%s",
            result.get("intent"),
            result.get("target_is_ai"),
            result.get("should_clarify"),
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
            bot_mentioned = is_bot_name_mentioned(current_user_text, bot_identity) or bool(re.search(r"<@!?\d+>", current_user_text))
            is_playful_request = looks_like_user_playful_request_or_command(current_user_text)
            # 発言内にBot自身の名前・愛称が含まれており、第三者伝聞（チクリ）でない場合は target_is_ai を True に保護
            if (bot_mentioned or is_playful_request or is_reply_request_to_bot) and intent_info.get("reason") != "heuristic_third_party_hearsay":
                intent_info["target_is_ai"] = True
            elif is_reply_to_other_user and not detected_reply_to_bot:
                # ユーザー同士の返信（is_reply_to_other_user）の場合、Botへの明示的メンションがない限り target_is_ai は False を死守
                intent_info["target_is_ai"] = False
            elif intent_info.get("reason") in ("heuristic_address_or_manner_tsukkomi", "heuristic_ai_utterance_confirmation") or is_ai_confirmation:
                # 直前のAI呼び方・口調ツッコミや自発言への確認・リアクションの場合、target_is_ai を True に保護
                intent_info["target_is_ai"] = True
            else:
                intent_info["target_is_ai"] = model_target_is_ai

            if is_playful_request or is_reply_request_to_bot:
                intent_info["target_is_ai"] = True
                intent_info["intent"] = "依頼"
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
                    log.info("[prompt-manager] Filtered out irrelevant fact_check/research memory: %s", line[:60])
                    continue
                filtered_lines.append(line)

            # 実態記憶の優先昇格：
            # Middle-LLMがメモリを選択したが、選ばれたものがすべてメタ記憶（意味不明・質問した等）で、
            # かつ候補メモリ内にユーザー発言に関連する実態記憶（出来事・感情）が存在する場合のみ差し替える
            from harness.agent.tool_registry.web_research.topic_investigation import is_meta_or_confused_memory
            if filtered_lines and all(is_meta_or_confused_memory(m) for m in filtered_lines):
                substantive_candidates = [
                    line for line in analyzer_memory_lines
                    if not is_meta_or_confused_memory(line)
                    and "以前ファクトチェックした話題" not in line
                    and "以前Web調査した話題" not in line
                ]
                if substantive_candidates:
                    user_substantive = _filter_memories_matching_user_text(
                        substantive_candidates, current_user_text, last_assistant_text
                    )
                    if user_substantive:
                        filtered_lines = user_substantive[:3]
                        log.info(
                            "[prompt-manager] Promoted substantive episodic memories over meta memories: %s",
                            filtered_lines,
                        )

            selected_lines = _post_filter_selected_memories(
                filtered_lines[:3],
                current_user_text,
                last_assistant_text,
                mentioned_users=mentioned_users,
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
        if looks_like_user_playful_request_or_command(current_user_text):
            if any(w in deliberation_info["reply_strategy"] for w in ("困惑", "皮肉", "突き放す", "拒絶", "無視", "スルー")):
                deliberation_info["reply_strategy"] = "要望受容・ツッコミ"
            deliberation_info["needs_clarification"] = False
            deliberation_info["uncertainty_level"] = "low"
            if deliberation_info["emotion_focus"] in ("confusion", "困惑"):
                deliberation_info["emotion_focus"] = ""
        elif is_reply_to_other_user:
            cur_strategy = str(deliberation_info.get("reply_strategy") or "").strip()
            if cur_strategy in ("受容", "direct_response", ""):
                compact_user = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", current_user_text, flags=re.DOTALL).strip()
                if any(k in compact_user for k in ("は？", "は?", "え？", "え?", "草", "なんで", "何で", "どういう")):
                    deliberation_info["reply_strategy"] = "外野ツッコミ"
                else:
                    deliberation_info["reply_strategy"] = "見守り"

        raw_avoid = str(result.get("avoid") or "").strip()
        if looks_like_user_playful_request_or_command(current_user_text) or is_reply_request_to_bot:
            if any(w in raw_avoid for w in ("指示", "命令", "拒絶", "断る", "無視", "過去の話題", "人間同士", "無理にAI宛て", "無理に自分宛て", "自分宛てと受け取る", "自分宛てと誤認")):
                deliberation_info["avoid"] = "頭ごなしな拒絶を避け、キャラクターらしく要望に乗っかる" if looks_like_user_playful_request_or_command(current_user_text) else "要望を受け止め、キャラクターらしくリアクションする"
            else:
                default_safe_avoid = "頭ごなしな拒絶を避け、キャラクターらしく要望に乗っかる" if looks_like_user_playful_request_or_command(current_user_text) else "要望を受け止め、キャラクターらしくリアクションする"
                deliberation_info["avoid"] = _clean_analyzer_note(raw_avoid, max_chars=50) or default_safe_avoid
        elif is_reply_to_other_user and any(w in raw_avoid for w in ("直前の話題", "前の話題", "話題に戻", "戻り続け", "直前の会話に戻", "過去の話題に戻", "話題（")):
            deliberation_info["avoid"] = "人間同士の文脈を無視したり無理に介入するのを避ける"
        elif is_reply_to_other_user and any(w in raw_avoid for w in ("AI宛て", "私宛て", "自分宛て")):
            deliberation_info["avoid"] = "人間同士の会話を自分宛てと誤認するのを避ける"
        elif any(w in raw_avoid for w in ("呼び捨て", "タメ口", "ため口", "敬語")):
            deliberation_info["avoid"] = "言い訳や自己弁護を避け、素直にリアクションする"
        elif any(w in raw_avoid for w in ("事実を説明", "事実を答える", "質問に答える", "命令に従って事実", "説明を避ける", "答えるのを避ける")):
            deliberation_info["avoid"] = "過度な長文や機械的な説明を避ける"
        else:
            deliberation_info["avoid"] = _clean_analyzer_note(raw_avoid, max_chars=50)
        raw_persona_risk = bool(result.get("persona_risk"))
        consistency_correction = _clean_analyzer_note(result.get("consistency_correction"), max_chars=50)

        if not raw_persona_risk:
            consistency_correction = ""

        _persona_forbidden_keywords = [
            "について", "話す", "伝える", "事実", "情報", "教える", "答える", "ユーザー", "相手", "ai", "bot", "呼ん", "呼ばれ", "認識",
            "場合", "なら", "設定", "質問", "返答", "メッセージ", "言っ", "いう", "こと", "する", "ため", "バグ", "報告", "表示", "アプデ", "アップデート", "ファクトチェック", "調査", "結論", "話題", "テーマ", "フシギ族",
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
        deliberation_info["topic_origin"] = _clean_analyzer_note(result.get("topic_origin"), max_chars=60)
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
            if _is_polluted(deliberation_info.get("reply_strategy", "")):
                deliberation_info["reply_strategy"] = "direct_response"
            if _is_polluted(deliberation_info.get("topic_origin", "")):
                deliberation_info["topic_origin"] = ""

    except Exception as e:
        log.warning("[prompt-manager] pre reply analyzer failed; falling back to heuristic: %r", e)

    if is_reply_request_to_bot or looks_like_user_playful_request_or_command(current_user_text):
        intent_info["target_is_ai"] = True
        intent_info["intent"] = "依頼"
        if any(w in str(deliberation_info.get("reply_strategy", "")) for w in ("困惑", "皮肉", "突き放す", "拒絶", "無視", "スルー", "外野", "見守り", "仲裁", "direct_response", "")):
            deliberation_info["reply_strategy"] = "要望受容・ツッコミ"
        deliberation_info["needs_clarification"] = False
        deliberation_info["uncertainty_level"] = "low"
        if deliberation_info.get("emotion_focus") in ("confusion", "困惑"):
            deliberation_info["emotion_focus"] = ""
        raw_avoid = str(deliberation_info.get("avoid") or "").strip()
        default_safe_avoid = "頭ごなしな拒絶を避け、キャラクターらしく要望に乗っかる" if looks_like_user_playful_request_or_command(current_user_text) else "要望を受け止め、キャラクターらしくリアクションする"
        if any(w in raw_avoid for w in ("人間同士", "無理にAI宛て", "無理に自分宛て", "自分宛てと受け取る", "自分宛てと誤認", "指示", "命令", "拒絶", "断る", "無視", "過去の話題")):
            deliberation_info["avoid"] = default_safe_avoid
        elif not raw_avoid:
            deliberation_info["avoid"] = default_safe_avoid

    if intent_info.get("reason") == "heuristic_third_party_hearsay":
        intent_info["target_is_ai"] = False
        deliberation_info["is_breakdown"] = False
        deliberation_info["breakdown_reason"] = ""
        deliberation_info["uncertainty_level"] = "low"
    elif is_ai_confirmation:
        intent_info["target_is_ai"] = True
        if intent_info.get("intent") in ("", "雑談", "statement", "unclear", "greeting", "monologue"):
            intent_info["intent"] = "確認"
        deliberation_info["reply_strategy"] = "肯定・スタンス維持"
        current_avoid = str(deliberation_info.get("avoid") or "").strip()
        if not current_avoid or "未体験" not in current_avoid:
            deliberation_info["avoid"] = "前言撤回や未体験者ぶる態度を避ける"
    elif is_user_deduction:
        if intent_info.get("intent") in ("", "statement", "unclear", "greeting", "monologue"):
            intent_info["intent"] = "確認"
        deliberation_info["is_breakdown"] = False
        deliberation_info["breakdown_reason"] = ""
        deliberation_info["reply_strategy"] = "受容・肯定"
        current_avoid = str(deliberation_info.get("avoid") or "").strip()
        if not current_avoid or any(kw in current_avoid for kw in ("勘違い", "否定", "反論", "訂正", "自己弁護")):
            deliberation_info["avoid"] = "不自然な反論や被害妄想を避け、まず肯定する"
        deliberation_info["uncertainty_level"] = "low"
        deliberation_info["needs_clarification"] = False

    if is_reply_to_other_user and not detected_reply_to_bot and not (is_bot_name_mentioned(current_user_text, bot_identity) or bool(re.search(r"<@!?\d+>", current_user_text))):
        intent_info["target_is_ai"] = False
        deliberation_info["is_breakdown"] = False
        deliberation_info["breakdown_reason"] = ""
        deliberation_info["persona_risk"] = False
        deliberation_info["consistency_correction"] = ""
        if str(deliberation_info.get("reply_strategy") or "").strip() in ("受容", "direct_response", ""):
            compact_user = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", current_user_text, flags=re.DOTALL).strip()
            if any(k in compact_user for k in ("は？", "は?", "え？", "え?", "草", "なんで", "何で", "どういう")):
                deliberation_info["reply_strategy"] = "外野ツッコミ"
            else:
                deliberation_info["reply_strategy"] = "見守り"
        cur_avoid = str(deliberation_info.get("avoid") or "").strip()
        if any(w in cur_avoid for w in ("直前の話題", "前の話題", "話題に戻", "戻り続け", "直前の会話に戻", "過去の話題に戻", "話題（")):
            deliberation_info["avoid"] = "人間同士の文脈を無視したり無理に介入するのを避ける"
        elif any(w in cur_avoid for w in ("AI宛て", "私宛て", "自分宛て")):
            deliberation_info["avoid"] = "人間同士の会話を自分宛てと誤認するのを避ける"

    # AI宛て発言またはBot宛て依頼の場合、人間同士扱い等の誤ったavoidを完全にサニタイズ
    if intent_info.get("target_is_ai") or is_reply_request_to_bot or detected_reply_to_bot:
        cur_avoid = str(deliberation_info.get("avoid") or "").strip()
        if any(w in cur_avoid for w in ("人間同士", "無理にAI宛て", "無理に自分宛て", "自分宛てと受け取る", "自分宛てと誤認")):
            deliberation_info["avoid"] = "要望を受け止め、キャラクターらしくリアクションする"

    selected_lines = sanitize_memory_candidates(selected_lines, last_assistant_text=last_assistant_text)
    return intent_info, selected_lines, channel_summary, deliberation_info


async def classify_mcp_routing(
    user_text: str,
    context_lines: list[str] | None = None,
    rule_hints: dict[str, Any] | None = None,
    model: str | None = None,
    timeout_sec: float = 4.0,
) -> dict[str, Any]:
    """小型分類モデル（middle-llm: Qwen3.5-9B, temperature=0, num_predict 256）を用いて、
    外部MCPツール（天気・Chrome・Web検索・ふたば・X等）の呼び出し要否と種別を高速分類する。
    """
    decision_model = model or _classifier_model_name()
    raw_text = str(user_text or "").strip()
    hints = dict(rule_hints or {})

    # 事前ヒューリスティック抽出
    url_match = _EXTERNAL_URL_RE.search(raw_text)
    has_url = bool(url_match)
    weather_keywords = ("天気", "気温", "雨", "傘", "台風", "予報", "降水確率", "晴れ", "曇り")
    has_weather_term = any(k in raw_text for k in weather_keywords)

    if "url_present" not in hints:
        hints["url_present"] = has_url
    if "weather_term_present" not in hints:
        hints["weather_term_present"] = has_weather_term

    # 明らかな空文字・短文挨拶・相槌の超高速バイパス（LLM呼び出し不要）
    compact = re.sub(r"\s+", "", raw_text)
    if not compact or compact in ("はい", "いいえ", "うん", "ううん", "草", "なるほど", "そっか", "わかった", "おはよう", "おやすみ", "こんにちは"):
        return {
            "needs_mcp": False,
            "mcp_category": "none",
            "tool_name": "",
            "query": "",
            "reason": "短文挨拶・相槌バイパス",
        }

    # middle-llm に問い合わせ
    try:
        prompt = build_mcp_routing_prompt(
            user_text=raw_text,
            context_lines=context_lines,
            rule_hints=hints,
        )
        result = await ollama_helpers.call_ollama_json(
            prompt,
            system_prompt=MCP_ROUTER_SYSTEM_PROMPT,
            schema=MCP_ROUTING_DECISION_SCHEMA,
            think=False,
            model=decision_model,
            timeout_sec=timeout_sec,
            retries=0,
            temperature=0.0,
            num_predict=256,
        )
        if isinstance(result, dict) and "needs_mcp" in result:
            needs_mcp = bool(result.get("needs_mcp"))
            category = str(result.get("mcp_category") or "none").strip()
            tool_name = str(result.get("tool_name") or "").strip()
            query = str(result.get("query") or "").strip()
            reason = str(result.get("reason") or "").strip()[:30]

            # 多重防御: 天気共感（「今日暑いね」など）の誤爆を事後ガード
            if category == "weather" and not any(k in raw_text for k in ("何", "どう", "教え", "？", "?", "度", "確率", "降る", "傘", "予報")):
                if any(k in raw_text for k in ("暑い", "寒い", "涼しい", "じめじめ", "いい天気", "青空")):
                    log.info("[prompt-manager] classify_mcp_routing: sanitized weather empathy to none for text=%r", raw_text)
                    return {
                        "needs_mcp": False,
                        "mcp_category": "none",
                        "tool_name": "",
                        "query": "",
                        "reason": "天候共感・感想のため不要",
                    }

            return {
                "needs_mcp": needs_mcp,
                "mcp_category": category if needs_mcp else "none",
                "tool_name": tool_name if needs_mcp else "",
                "query": query if needs_mcp else "",
                "reason": reason,
            }
    except Exception as e:
        log.warning("[prompt-manager] classify_mcp_routing call_ollama_json failed: %r; using fallback rules", e)

    # フォールバック（ルールベース判定）
    if has_url and url_match:
        return {
            "needs_mcp": True,
            "mcp_category": "chrome_browser",
            "tool_name": "chrome_read_url",
            "query": url_match.group(0),
            "reason": "URL検出フォールバック",
        }
    if has_weather_term and any(q in raw_text for q in ("何", "どう", "教え", "？", "?", "度", "降る", "傘", "予報")):
        return {
            "needs_mcp": True,
            "mcp_category": "weather",
            "tool_name": "jma_get_weather",
            "query": raw_text,
            "reason": "天気照会フォールバック",
        }

    return {
        "needs_mcp": False,
        "mcp_category": "none",
        "tool_name": "",
        "query": "",
        "reason": "通常対話フォールバック",
    }

