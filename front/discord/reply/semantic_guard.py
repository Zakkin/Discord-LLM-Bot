"""危険信号判定および中位モデルによる意味レベル返信ガード"""
from __future__ import annotations

import difflib
import importlib
import json
import logging
import re
from typing import Any, Awaitable, Callable

from ollama_bot.common.config_helpers import cfg, cfg_bool

log = logging.getLogger("front.discord.reply.semantic_guard")

_image_claims_extract_captions: Callable[[list[str]], str] | None = None
_image_claims_find_ungrounded: Callable[[str, str], list[str]] | None = None
try:
    from harness.agent.prompt_parser.parser.image_claims import (
        extract_bracketed_image_captions as _imported_extract_bracketed_image_captions,
        find_ungrounded_image_claims as _imported_find_ungrounded_image_claims,
    )
    _image_claims_extract_captions = _imported_extract_bracketed_image_captions
    _image_claims_find_ungrounded = _imported_find_ungrounded_image_claims
except ImportError:
    pass

extract_bracketed_image_captions: Callable[[list[str]], str] | None = _image_claims_extract_captions
find_ungrounded_image_claims: Callable[[str, str], list[str]] | None = _image_claims_find_ungrounded

_conversation_is_active: Callable[..., bool] | None = None
try:
    from harness.agent.prompt_parser.prompt_manager.conversation_activity import (
        is_conversation_active as _imported_is_conversation_active,
    )
    _conversation_is_active = _imported_is_conversation_active
except ImportError:
    pass


def resolve_inspection_model() -> tuple[str, str]:
    """意味検査に使うモデルと、その選択理由を返す（§170）。

    会話中は生成直後で main が常駐しているため、会話活動レジストリが
    アクティブなら main に寄せ、モデル入替待ちによる検査タイムアウト
    （fail-open の素通り）を避ける。それ以外は従来どおり middle。
    """
    middle = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm")
    if not cfg_bool("SEMANTIC_GUARD_MAIN_WHEN_ACTIVE_ENABLED", True):
        return middle, "default"
    if _conversation_is_active is not None:
        try:
            window = float(cfg("SEMANTIC_GUARD_MAIN_ACTIVE_WINDOW_SEC", 180) or 180)
            if _conversation_is_active(window_sec=window):
                main = str(cfg("OLLAMA_MODEL", "main-llm") or "main-llm")
                if main:
                    return main, "conversation_active"
        except Exception:
            pass
    return middle, "default"


def _resolve_image_claims_fns() -> tuple[
    Callable[[list[str]], str] | None,
    Callable[[str, str], list[str]] | None,
]:
    """共有モジュール（§152）の純関数を解決する（未着時は fail-open で None を返す）。"""
    ext_fn = extract_bracketed_image_captions
    find_fn = find_ungrounded_image_claims
    if ext_fn is not None and find_fn is not None:
        return ext_fn, find_fn
    try:
        ic_mod = importlib.import_module("harness.agent.prompt_parser.parser.image_claims")
        if ext_fn is None:
            cand_ext = getattr(ic_mod, "extract_bracketed_image_captions", None)
            if callable(cand_ext):
                ext_fn = cand_ext
        if find_fn is None:
            cand_find = getattr(ic_mod, "find_ungrounded_image_claims", None)
            if callable(cand_find):
                find_fn = cand_find
    except ImportError:
        pass
    return ext_fn, find_fn

SEMANTIC_INSPECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasserts_denied": {"type": "boolean"},
        "subject_swap": {"type": "boolean"},
        "contradicts_latest_user": {"type": "boolean"},
        "actor_mismatch": {"type": "boolean"},
        "self_contradiction": {"type": "boolean"},
        "incoherent": {"type": "boolean"},
        "self_projection": {"type": "boolean"},
        "premise_flip": {"type": "boolean"},
        "addressee_self_insertion": {"type": "boolean"},
        "target_claim_mismatch": {"type": "boolean"},
        "overliteral_reading": {"type": "boolean"},
        "ungrounded_image_text": {"type": "boolean"},
        "ok": {"type": "boolean"},
    },
    "required": [
        "reasserts_denied",
        "subject_swap",
        "contradicts_latest_user",
        "actor_mismatch",
        "self_contradiction",
        "incoherent",
        "self_projection",
        "premise_flip",
        "addressee_self_insertion",
        "target_claim_mismatch",
        "overliteral_reading",
        "ungrounded_image_text",
        "ok",
    ],
}

_SEMANTIC_VIOLATION_KEYS: tuple[str, ...] = (
    "reasserts_denied",
    "subject_swap",
    "contradicts_latest_user",
    "actor_mismatch",
    "self_contradiction",
    "incoherent",
    "self_projection",
    "premise_flip",
    "addressee_self_insertion",
    "target_claim_mismatch",
    "overliteral_reading",
    "ungrounded_image_text",
)


def extract_recent_events_from_runtime(runtime: Any) -> list[dict[str, Any]] | None:
    """MessageRuntime.working_context から直近の出来事メモリストを抽出する。"""
    working_ctx = getattr(runtime, "working_context", None)
    if not isinstance(working_ctx, dict):
        return None
    raw_events = working_ctx.get("recent_events")
    if isinstance(raw_events, list):
        return [item for item in raw_events if isinstance(item, dict)]
    conv_st = working_ctx.get("conversation_state")
    if isinstance(conv_st, dict):
        raw_st_events = conv_st.get("recent_events")
        if isinstance(raw_st_events, list):
            return [item for item in raw_st_events if isinstance(item, dict)]
    return None


def extract_semantic_violation_flags(insp: dict[str, Any] | None) -> list[str]:
    """意味検査結果 dict から違反フラグ名一覧を抽出する。"""
    if not isinstance(insp, dict):
        return []
    flags = [key for key in _SEMANTIC_VIOLATION_KEYS if insp.get(key)]
    if not flags and not insp.get("ok", True):
        flags.append("ok=False")
    return flags

_ACTOR_ATTRIBUTION_RE = re.compile(
    r"(?:に(?:奪わ|取ら|言わ|教え|もら|され)|が(?:持って|言っ|やっ|奪っ)た)"
)
_NAME_PREFIX_ACTOR_RE = re.compile(
    r"([ぁ-んァ-ヶー\u4e00-\u9fa5a-zA-Z0-9_]{2,})(?:に(?:奪わ|取ら|言わ|教え|もら|され)|が(?:持って|言っ|やっ|奪っ)た)"
)
_SELF_PROJECTION_RISK_RE = re.compile(r"(?:ワイ|私|僕|俺|自分|あたし|わたし|うち)も")
_QUESTION_ENDINGS: tuple[str, ...] = ("のか", "の？", "ですか", "ますか", "かな")
_QUESTION_INVERTED_RE = re.compile(
    r"(?<![こそあど])のか[\s　]*(?!(?:分か|わか|知|しら|しっ|気にな|きにな|忘れ|わすれ|覚え|おぼえ|教え|おしえ|思|おも|聞|言|書|読))[^\s。！？!?\n]{1,5}$"
)
_QUESTION_POLITE_RE = re.compile(r"(?:ですか|ますか)")
_KATAKANA_TOKEN_RE = re.compile(r"[ァ-ヶー]{2,}")
_KANJI_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5々]{2,}")
_KANJI_STEM_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5々][ぁ-ん]")
_HIRAGANA_TOKEN_RE = re.compile(r"[ぁ-んー]{3,}")
_HIRAGANA_STOPWORDS: frozenset[str] = frozenset({
    "です",
    "ます",
    "なのか",
    "ました",
    "だった",
    "ですか",
    "ますか",
    "ません",
    "でした",
    "だけど",
    "けれど",
    "しかし",
    "だから",
    "なので",
    "それで",
    "そして",
    "という",
    "のこと",
    "ですかね",
    "のかな",
    "んです",
    "のです",
    "あった",
    "なった",
    "ましたか",
    "でしたか",
    "だったの",
    "おったんか",
    "ている",
    "ていた",
})
_PERCEPTION_CLAIM_RE = re.compile(r"(?:見え|聞こえ)(?:て|ます|ない|ません|る|た|ん)")
_PERCEPTION_USER_WORDS: tuple[str, ...] = ("見", "聞", "音", "目", "耳", "声")
_QUOTE_SPEECH_CLAIM_RE = re.compile(
    r"(?:「[^」]+」|『[^』]+』)\s*(?:って|と)?\s*(?:言っ(?:た|て|と)|言いまし|言う(?:た|て))"
)
_PAST_ACTION_CLAIM_RE = re.compile(
    r"(?:今さっき|さっき|先ほど|先程|さきほど|こないだ|この前|前に|まえに|以前)"
    r"[^。！？!?\n]{0,30}?"
    r"(?:言っ(?:た|て|と)|言いまし|貼っ(?:た|て)|貼りまし|送っ(?:た|て)|送りまし"
    r"|見せ(?:た|て|まし)|書い(?:た|て)|書きまし|出し(?:た|て)|出しまし|話し(?:た|て)|話しまし)"
)
# 受け取り framing（感謝・称賛・呼びかけ等を受けた体裁）検知用（addressee_claim_risk トリガー）
_ADDRESSEE_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:ありがとう|感謝|お礼)(?:って|と|なんて|を)?\s*(?:言[っう]て(?:もら|貰)(?:え|っ|い)|言われ(?:て|た|る|ちゃ|まし|たら)|(?:もら|貰)(?:え|っ|い)|(?:いただ|頂)(?:い|け))"
    r"|(?:言[っう]|声を?かけ|声掛け|褒め|ほめ|誘っ|さそっ|感謝し|労っ|ねぎらっ|祝っ)て(?:もら|貰)(?:え|っ|い)"
    r"|(?:呼|よ)んで(?:もら|貰)(?:え|っ|い)"
    r"|(?:言わ|褒めら|ほめら|誘わ|さそわ|呼ば|よば|声を?かけら|声掛けら|感謝さ|労わ|ねぎらわ|祝わ)れ(?:て|た|る|ちゃ|まし|たら)"
    r")"
)
# 返信の論点ずれ（引用元・第三者の主張への反論）検知用（target_claim_mismatch_risk トリガー）
_TARGET_CLAIM_QUOTED_PHRASE_RE = re.compile(r"(?:「([^」\n]{2,})」|『([^』\n]{2,})』)")
_TARGET_CLAIM_DISAGREEMENT_MARKERS: tuple[str, ...] = (
    "言い切れ",
    "とは限らない",
    "はずがない",
    "わけがない",
    "そんなことない",
    "違う",
    "読めない",
    "決めつけ",
    "決め付け",
    "とは言えない",
    "無理がある",
    "成り立たない",
    "当てはまらない",
)
_TARGET_CLAIM_DISAGREEMENT_RE = re.compile(
    "|".join(re.escape(m) for m in _TARGET_CLAIM_DISAGREEMENT_MARKERS)
)
_CONTEXT_SPEAKER_LINE_RE = re.compile(r"^([^:/]+?):(?!/)\s*(.*)$")
# 字義通りの誤読（語釈・否定定義・検証式留保）検知用（literal_gloss_risk トリガー）
_LITERAL_GLOSS_NEGATION_RE = re.compile(
    r"って[、,]?\s*[^。\n]{0,40}のこと(?:じゃなくて|ではなく|とは違)"
)
_LITERAL_GLOSS_DEFINITION_RE = re.compile(
    r"(?:というのは|とは)[^。\n]{0,60}(?:のこと|を意味し|を指し)"
)
_VERIFICATION_HEDGE_PHRASES: tuple[str, ...] = (
    "確認できた範囲では",
    "確認できる範囲では",
    "確認した範囲では",
    "確認した限りでは",
    "確認できていません",
    "確認が取れていません",
)


def _has_literal_gloss_risk(reply_text: str) -> bool:
    """字義通りの誤読・検証式留保検査の決定的トリガー（literal_gloss_risk）を判定する。"""
    if not reply_text:
        return False
    if _LITERAL_GLOSS_NEGATION_RE.search(reply_text):
        return True
    if _LITERAL_GLOSS_DEFINITION_RE.search(reply_text):
        return True
    return any(phrase in reply_text for phrase in _VERIFICATION_HEDGE_PHRASES)


def _extract_context_body_for_claim_check(line: str) -> str:
    """context_lines の1行から話者プレフィックス等を除いた本文部分を抽出する。"""
    stripped = str(line or "").strip()
    if not stripped:
        return ""
    cleaned = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", stripped)
    m_spk = _CONTEXT_SPEAKER_LINE_RE.match(cleaned)
    if m_spk:
        spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", m_spk.group(1)).strip()
        if spk in ("System", "system"):
            return ""
        body = m_spk.group(2).strip()
        return re.sub(r"\s*\[Botリアクション:[^\]]*\]\s*$", "", body).strip()
    return cleaned


def _split_user_text_quote_and_own(text: str) -> tuple[str, list[str]]:
    """ユーザー発言から引用行（> / ＞ 開始行）と本人の主張本文を分離する。"""
    own_lines: list[str] = []
    quoted_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        s = raw_line.strip()
        if not s:
            continue
        if s.startswith((">", "＞")):
            quoted_lines.append(s.lstrip(">＞").strip())
        else:
            own_lines.append(s)
    if not own_lines:
        return str(text or "").strip(), []
    return "\n".join(own_lines), quoted_lines


def _has_target_claim_mismatch_risk(
    *,
    reply_text: str,
    target_user_text: str | None,
    context_lines: list[str] | None,
) -> bool:
    """論点ずれ検査の決定的トリガー（target_claim_mismatch_risk）を判定する。"""
    if not reply_text or not target_user_text:
        return False
    if not _TARGET_CLAIM_DISAGREEMENT_RE.search(reply_text):
        return False

    quoted_phrases: list[str] = []
    for m in _TARGET_CLAIM_QUOTED_PHRASE_RE.finditer(reply_text):
        phrase = (m.group(1) or m.group(2) or "").strip()
        if len(phrase) >= 2:
            quoted_phrases.append(phrase)
    if not quoted_phrases:
        return False

    latest_own_text, quoted_prior_lines = _split_user_text_quote_and_own(target_user_text)
    if not latest_own_text:
        return False

    prior_bodies: list[str] = [q for q in quoted_prior_lines if q]
    if context_lines:
        for cl in context_lines:
            for raw_line in str(cl or "").splitlines():
                body = _extract_context_body_for_claim_check(raw_line)
                if body:
                    prior_bodies.append(body)
    if not prior_bodies:
        return False

    for phrase in quoted_phrases:
        if phrase in latest_own_text:
            continue
        if any(phrase in body for body in prior_bodies):
            return True
    return False


def _extract_latest_speaker_text(
    context_lines: list[str] | None,
    current_speaker: str | None,
) -> str | None:
    """context_lines から current_speaker が最後に発話した本文を抽出する。"""
    speaker_target = str(current_speaker or "").strip()
    if not context_lines or not speaker_target:
        return None
    latest_body: str | None = None
    for cl in context_lines:
        for raw_line in str(cl or "").splitlines():
            cl_clean = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", raw_line.strip())
            if ":" not in cl_clean:
                continue
            spk_part, body = cl_clean.split(":", 1)
            spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", spk_part).strip()
            if spk == speaker_target:
                cleaned_body = re.sub(r"\s*\[Botリアクション:[^\]]*\]\s*$", "", body.strip())
                latest_body = cleaned_body.strip()
    return latest_body


def _is_question_utterance(text: str) -> bool:
    """対象ユーザー発言が質問形（疑問符・質問文末・倒置「のか」・ですか/ますか）かを判定する。"""
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if "？" in stripped or "?" in stripped:
        return True
    if stripped.endswith(_QUESTION_ENDINGS):
        return True
    if _QUESTION_POLITE_RE.search(stripped):
        return True
    return bool(_QUESTION_INVERTED_RE.search(stripped))


def extract_non_answer_tokens(text: str) -> list[str]:
    """質問文から non_answer_risk 判定用のトピックトークンを抽出する。"""
    if not text:
        return []
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        if tok and tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for m in _KATAKANA_TOKEN_RE.finditer(text):
        _add(m.group(0))
    for m in _KANJI_TOKEN_RE.finditer(text):
        _add(m.group(0))
    # 名詞的トークン（カタカナ2文字以上・漢字2文字以上）が得られた場合はそれを優先し、
    # 動詞語幹への誤一致によるすり抜け（例:「ダイス回せたのか」に対し「回せへんかった」等）を防ぐ。
    if tokens:
        return tokens

    for m in _KANJI_STEM_TOKEN_RE.finditer(text):
        _add(m.group(0))
    for m in _HIRAGANA_TOKEN_RE.finditer(text):
        word = m.group(0)
        if word.strip("ー") and word not in _HIRAGANA_STOPWORDS:
            _add(word)
    return tokens


def assess_reply_risk(
    *,
    reply_text: str,
    intent_info: dict[str, Any] | None = None,
    denied_claims: list[dict[str, str]] | None = None,
    previous_reply_text: str | None = None,
    context_lines: list[str] | None = None,
    current_speaker: str | None = None,
    bot_name: str | None = None,
    user_text: str | None = None,
) -> dict[str, Any]:
    needs_check = False
    reasons: list[str] = []
    substring_hit: str | None = None

    # 1. 今ターンの訂正がある
    if intent_info and intent_info.get("user_correction"):
        needs_check = True
        reasons.append("turn_has_user_correction")

    # 2. 否定済みレジストリが存在
    claims = denied_claims or []
    if claims:
        needs_check = True
        reasons.append("has_denied_claims")
        # 部分文字列の即時違反チェック（LLM不要）
        norm_reply = re.sub(r"[\s、。！？!?,.]+", "", reply_text).lower()
        for c in claims:
            c_text = c.get("claim", "")
            if len(c_text) >= 8:
                norm_c = re.sub(r"[\s、。！？!?,.]+", "", c_text).lower()
                if norm_c in norm_reply:
                    substring_hit = c_text
                    break

    # 3. 直前返信との類似度
    if previous_reply_text:
        sim = difflib.SequenceMatcher(None, reply_text, previous_reply_text).ratio()
        if sim >= 0.35:
            needs_check = True
            reasons.append(f"similarity_high_{sim:.2f}")

    # 4. 行為者帰属リスク検査
    if cfg_bool("ACTOR_CHECK_ENABLED", True) and (context_lines or reply_text):
        speakers: set[str] = set()
        if context_lines:
            for cl in context_lines:
                cl_clean = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", str(cl).strip())
                if ":" in cl_clean:
                    spk_part = cl_clean.split(":", 1)[0].strip()
                    spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", spk_part).strip()
                    if spk and spk not in ("System", "system", "Assistant", "assistant"):
                        speakers.add(spk)

        excluded = {"私", "僕", "俺", "自分", "あなた", "君", "お前"}
        if current_speaker:
            excluded.add(current_speaker.strip())
        if bot_name:
            excluded.add(bot_name.strip())

        third_parties = {s for s in speakers if s and s not in excluded}

        sentences = [s.strip() for s in re.split(r"[。\n！？!?]+", reply_text) if s.strip()]
        actor_hit = False
        for sent in sentences:
            # 1) 文中に会話履歴の第三者名が存在し、かつ過去行為帰属表現がある
            has_tp = any(tp in sent for tp in third_parties)
            if has_tp and _ACTOR_ATTRIBUTION_RE.search(sent):
                actor_hit = True
                break
            # 2) 行為帰属表現の直前に人名/名詞（第三者）が存在する
            m_actor = _NAME_PREFIX_ACTOR_RE.search(sent)
            if m_actor:
                cand_name = m_actor.group(1).strip()
                if cand_name and cand_name not in excluded:
                    actor_hit = True
                    break

        if actor_hit:
            needs_check = True
            reasons.append("actor_attribution_risk")

    target_user_text = (
        user_text
        if user_text is not None
        else _extract_latest_speaker_text(context_lines, current_speaker)
    )

    # 5. 自己投影ミラーリングリスク検査
    if reply_text and _SELF_PROJECTION_RISK_RE.search(reply_text):
        needs_check = True
        reasons.append("self_projection_risk")

    # 6. 質問への無回答リスク検査
    if target_user_text is not None and _is_question_utterance(target_user_text):
        topic_tokens = extract_non_answer_tokens(target_user_text)
        if topic_tokens and not any(tok in reply_text for tok in topic_tokens):
            needs_check = True
            reasons.append("non_answer_risk")

    # 7. 無前提な知覚の主張リスク検査
    if (
        target_user_text is not None
        and reply_text
        and _PERCEPTION_CLAIM_RE.search(reply_text)
        and not any(w in target_user_text for w in _PERCEPTION_USER_WORDS)
    ):
        needs_check = True
        reasons.append("unprompted_perception_claim")

    # 8. 過去の発言・行動の主張リスク検査
    if reply_text and (
        _QUOTE_SPEECH_CLAIM_RE.search(reply_text)
        or _PAST_ACTION_CLAIM_RE.search(reply_text)
    ):
        needs_check = True
        reasons.append("previous_statement_claim")

    # 9. 宛先への自己挿入（受け取り framing）リスク検査
    if (
        cfg_bool("ADDRESSEE_CHECK_ENABLED", True)
        and reply_text
        and _ADDRESSEE_CLAIM_RE.search(reply_text)
    ):
        needs_check = True
        reasons.append("addressee_claim_risk")

    # 10. 返信の論点ずれ（引用元・第三者の主張への反論）リスク検査
    if cfg_bool("TARGET_CLAIM_CHECK_ENABLED", True) and _has_target_claim_mismatch_risk(
        reply_text=reply_text,
        target_user_text=target_user_text,
        context_lines=context_lines,
    ):
        needs_check = True
        reasons.append("target_claim_mismatch_risk")

    # 11. 字義通りの誤読（語釈・検証式留保）リスク検査
    if cfg_bool("LITERAL_GLOSS_CHECK_ENABLED", True) and _has_literal_gloss_risk(reply_text):
        needs_check = True
        reasons.append("literal_gloss_risk")

    # 12. 画像内テキスト引用の未接地リスク検査（§152 共有チェック）
    if cfg_bool("IMAGE_CLAIM_CHECK_ENABLED", True) and context_lines and reply_text:
        ext_captions_fn, find_ungrounded_fn = _resolve_image_claims_fns()
        if ext_captions_fn is not None and find_ungrounded_fn is not None:
            try:
                captions = ext_captions_fn(list(context_lines))
                if captions:
                    ungrounded = find_ungrounded_fn(reply_text, captions)
                    if ungrounded:
                        needs_check = True
                        reasons.append("ungrounded_image_claim")
            except Exception as e:
                log.debug("[SEMANTIC_GUARD] image claim check skipped (fail-open): %r", e)

    return {
        "needs_check": needs_check,
        "reasons": reasons,
        "substring_hit": substring_hit,
    }


_LESSONS_BULLET_PREFIXES: tuple[str, ...] = ("・", "- ", "* ", "• ")


def _is_lessons_heading_line(line: str) -> bool:
    """lessons_block の先頭行が見出し行かを判定する。"""
    s = line.strip()
    if not s or s.startswith(_LESSONS_BULLET_PREFIXES):
        return False
    if s.startswith(("#", "■", "◆", "▼")):
        return True
    if (
        (s.startswith("【") and s.endswith("】"))
        or (s.startswith("[") and s.endswith("]"))
        or (s.startswith("［") and s.endswith("］"))
    ):
        return True
    return s.endswith((":", "："))


def _extract_lessons_block_body(lessons_block: str | None) -> str:
    """lessons_block から見出し行を除いた本文を抽出する（空または本文なしなら空文字）。"""
    raw = str(lessons_block or "").strip()
    if not raw:
        return ""
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return "" if _is_lessons_heading_line(lines[0]) else lines[0]
    first = lines[0]
    if not first.startswith(_LESSONS_BULLET_PREFIXES) and (
        _is_lessons_heading_line(first)
        or lines[1].startswith(_LESSONS_BULLET_PREFIXES)
    ):
        lines = lines[1:]
    return "\n".join(lines).strip()


async def inspect_reply_semantics(
    *,
    llm_caller_fn: Callable[[str], Awaitable[Any]],
    user_text: str,
    bot_previous_text: str,
    generated_reply: str,
    denied_claims: list[dict[str, str]],
    context_lines: list[str] | None = None,
    recent_events: list[dict[str, Any]] | None = None,
    bot_name: str | None = None,
    lessons_block: str = "",
) -> dict[str, bool]:
    """中位モデルで固定JSON意味検査を実行"""
    prompt_parts: list[str] = []
    bot_name_clean = str(bot_name or "").strip()
    if bot_name_clean:
        prompt_parts.append(f"Bot 本人の表示名: {bot_name_clean}（会話履歴では Assistant と表記される）")
    prompt_parts.extend([
        f"ユーザー直前発言: {user_text}",
        f"Bot直前発言: {bot_previous_text}",
        f"生成された返信: {generated_reply}",
        f"否定済みリスト: {[c.get('claim') for c in denied_claims]}",
    ])
    if context_lines:
        recent_ctx = context_lines[-10:]
        prompt_parts.append("会話履歴（直近最大10行）:\n" + "\n".join(recent_ctx))
    if recent_events:
        ev_lines = []
        for ev in recent_events[-5:]:
            at_str = f"[{ev.get('at', '')}] " if ev.get("at") else ""
            target_str = f"（相手: {ev.get('target')}）" if ev.get("target") else ""
            ev_lines.append(f"- {at_str}{ev.get('actor', '不明')} が {ev.get('action', '')}{target_str}")
        prompt_parts.append("出来事メモ:\n" + "\n".join(ev_lines))

    prompt_parts.append(
        "以下の12項目について検査しJSONで答えなさい:\n"
        "1. reasserts_denied: 否定済みリストの内容を言い換え含めて再主張しているか\n"
        "2. subject_swap: 誰が何をしたか主語や立場を取り違えているか\n"
        "3. contradicts_latest_user: 直近ユーザー発言と矛盾する前提か\n"
        "4. actor_mismatch: 返信が言及した行為者（誰がやったか）が、会話履歴や出来事メモの記録と食い違っているか\n"
        "5. self_contradiction: 生成された返信が、Bot自身の直前発言の主張と矛盾している場合に true（例: 直前に「できる」と言ったことを、理由なく「できない」と覆す）。相手発言との矛盾は contradicts_latest_user の担当であり、ここでは扱わない\n"
        "6. incoherent: 主述関係が崩れている、単語の並びが日本語として意味をなさない、直前発言と無関係な固有名詞が唐突に混入している、存在しない言い回し・造語が混入している（例: 「今一線」）、文頭の欠落など文としての体裁が崩れた断片になっている、数値・単位・専門用語の組み合わせがその分野で成立しない（例: 競馬で「300m逃げ切る」）、話題と無関係な例えや別ジャンルの名詞の差し込み（例: ダイスの話題への『ガチャ爆死』）等、文として意味が通らない場合に true。口調・方言・スラング・キャラ付けの誇張は文として意味が通れば false\n"
        "7. self_projection: 生成返信が、ユーザーが直前に述べた体験・知識・状態・属性を、会話履歴・Bot の過去発言・ペルソナ設定に裏付けがないのに、Bot 自身の体験・知識・状態として主張している場合に true。裏付けのある自己言及（Bot 自身の過去発言や設定に基づくもの）や、単なる相槌・共感表現は false\n"
        "8. premise_flip: ユーザーが質問・確認している事柄について、生成返信がその質問に答えず、根拠なく反対の前提（できなかった・失敗した・知らない等）をユーザーに帰属させている場合に true。質問が問うていない事柄へのすり替え（例: 生存を問われて『見えています』と視覚の状態で答える）も premise_flip に含む。質問への回答や、会話履歴に根拠のある指摘は false\n"
        "9. addressee_self_insertion: 生成返信が、Bot 自身が感謝・称賛・謝罪・呼びかけ等の発話行為の宛先（受取人）である前提で書かれている場合に true。ただし、ユーザー直前発言や会話履歴で、その発話行為の宛先が Bot 本人以外の特定の相手に限定されており（例: 『Aさんありがとう、Bさんありがとう』と名前を列挙して感謝している）、Bot 本人がその宛先に含まれていない場合に限る。Bot 本人は会話履歴で Assistant と表記される参加者であり、表示名での名指しも Bot 本人への宛先に含む。他の Bot（例: 川柳検出BOT）は Bot 本人ではない。宛先に Bot 本人が含まれている場合、宛先が限定されておらず直前のやり取りに Bot が参加している場合、単なる相槌・共感は false\n"
        "10. target_claim_mismatch: 生成返信が、最新の発言者が主張していない内容（引用された過去発言・第三者の主張）に反論または回答しており、かつ最新の発言者自身の主張には実質的に触れていない場合に true。最新の発言者の主張への直接の応答である場合、最新の発言中の言葉を引用して反論・同意する場合、引用元の話に触れていても最新の発言者の主張への応答が主である場合は false\n"
        "11. overliteral_reading: ユーザーのカジュアルな発言に含まれるスラング・造語・接尾辞的な用法（例: 『ぬいぐるみチック』の『チック』は『〜っぽい』の意）を、専門用語や字義通りの意味に誤読して用語解説・講釈をしている場合に true。また、冗談や軽い推測（例: 『ワンチャン住人説濃厚』）に対して『確認できた範囲ではわかりません』のような検証・裏付けの物差しを持ち込んで留保している場合も true。ユーザーが実際に定義や事実確認を求めている場合、および調査・ファクトチェックの文脈で根拠を留保している場合は false\n"
        "12. ungrounded_image_text: 生成返信が画像に書かれているとして引用した文字列が、会話履歴に含まれる画像内容キャプション（[画像内容: ...]）のどこにも見当たらず、他の発言者の引用でもない場合に true。キャプションに実在する文字列の引用、画像の見た目に対する感想や推測は false\n"
        '{"reasserts_denied": false, "subject_swap": false, "contradicts_latest_user": false, "actor_mismatch": false, "self_contradiction": false, "incoherent": false, "self_projection": false, "premise_flip": false, "addressee_self_insertion": false, "target_claim_mismatch": false, "overliteral_reading": false, "ungrounded_image_text": false, "ok": true}'
    )
    lessons_body = _extract_lessons_block_body(lessons_block)
    if lessons_body:
        prompt_parts.append(
            "過去の振り返りからの注意点（補助材料）:\n"
            f"{lessons_body}\n"
            "上記の注意点に当てはまる振る舞いが候補文にある場合のみ、"
            "対応するカテゴリの判定材料にすること。"
            "当てはまらなければ無視し、注意点にない違反を新たに作らないこと。"
        )
    prompt = "\n".join(prompt_parts)
    try:
        res = await llm_caller_fn(prompt)
        if isinstance(res, str):
            clean_res = res.strip()
            if "```json" in clean_res:
                clean_res = clean_res.split("```json", 1)[1].split("```", 1)[0].strip()
            elif "```" in clean_res:
                clean_res = clean_res.split("```", 1)[1].split("```", 1)[0].strip()
            try:
                res = json.loads(clean_res)
            except Exception:
                pass
        if isinstance(res, dict) and (
            "ok" in res
            or "incoherent" in res
            or "self_contradiction" in res
            or "self_projection" in res
            or "premise_flip" in res
            or "addressee_self_insertion" in res
            or "target_claim_mismatch" in res
            or "overliteral_reading" in res
            or "ungrounded_image_text" in res
        ):
            log.info("[SEMANTIC_GUARD] inspection result: %r", res)
            reasserts = bool(res.get("reasserts_denied"))
            subject_swap = bool(res.get("subject_swap"))
            contradicts = bool(res.get("contradicts_latest_user"))
            actor_mismatch = bool(res.get("actor_mismatch"))
            self_contradiction = bool(res.get("self_contradiction"))
            incoherent = bool(res.get("incoherent"))
            self_projection = bool(res.get("self_projection"))
            premise_flip = bool(res.get("premise_flip"))
            addressee_self_insertion = bool(res.get("addressee_self_insertion"))
            target_claim_mismatch = (
                bool(res.get("target_claim_mismatch"))
                if cfg_bool("TARGET_CLAIM_CHECK_ENABLED", True)
                else False
            )
            overliteral_reading = bool(res.get("overliteral_reading"))
            ungrounded_image_text = bool(res.get("ungrounded_image_text"))
            raw_ok = res.get("ok", True)
            ok_val = bool(raw_ok) and not (
                reasserts
                or subject_swap
                or contradicts
                or actor_mismatch
                or self_contradiction
                or incoherent
                or self_projection
                or premise_flip
                or addressee_self_insertion
                or target_claim_mismatch
                or overliteral_reading
                or ungrounded_image_text
            )
            return {
                "reasserts_denied": reasserts,
                "subject_swap": subject_swap,
                "contradicts_latest_user": contradicts,
                "actor_mismatch": actor_mismatch,
                "self_contradiction": self_contradiction,
                "incoherent": incoherent,
                "self_projection": self_projection,
                "premise_flip": premise_flip,
                "addressee_self_insertion": addressee_self_insertion,
                "target_claim_mismatch": target_claim_mismatch,
                "overliteral_reading": overliteral_reading,
                "ungrounded_image_text": ungrounded_image_text,
                "ok": ok_val,
            }
    except Exception as e:
        log.warning("[SEMANTIC_GUARD] inspection failed (fallback ok=True): %r", e)
    return {
        "reasserts_denied": False,
        "subject_swap": False,
        "contradicts_latest_user": False,
        "actor_mismatch": False,
        "self_contradiction": False,
        "incoherent": False,
        "self_projection": False,
        "premise_flip": False,
        "addressee_self_insertion": False,
        "target_claim_mismatch": False,
        "overliteral_reading": False,
        "ungrounded_image_text": False,
        "ok": True,
    }
