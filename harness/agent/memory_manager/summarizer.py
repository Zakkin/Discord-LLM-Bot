"""
[AI Agent Summary]
ユーザー習慣プロファイル更新、学習候補データ保存、チャンネル要約更新を担当するモジュール。
This module handles user habit profile updates, training candidate persistence, and channel summaries.
"""
from __future__ import annotations

import inspect
import logging
import re
from typing import Any

from .compat import get_ollama_helpers
from .store import MemoryStore
from .extractor import (
    _get_unsafe_or_banter_filter_reason,
    _is_fallback_reply,
    _is_ignorable_call_error,
    _join_context_lines,
    _persona_from_cfg,
)
from ollama_bot.common import ollama_helpers

log = logging.getLogger("harness.agent.memory_manager.summarizer")
logger = log

_DISCORD_RAW_ID_RE = re.compile(r"\b\d{17,20}\b|(?<!\d)\d{17,20}(?!\d)")


def _mask_sensitive_log_text(
    text: Any,
    *,
    user_id: int | str | None = None,
    user_names: list[str | None] | tuple[str | None, ...] = (),
) -> str:
    """ログ出力に含まれる17〜20桁のDiscord生IDやユーザー名をマスクする。"""
    s = str(text or "")
    if not s:
        return ""
    if user_id is not None:
        uid_str = str(user_id).strip()
        if uid_str:
            s = s.replace(uid_str, "[MASKED_ID]")
    s = _DISCORD_RAW_ID_RE.sub("[MASKED_ID]", s)
    for name in user_names:
        clean = str(name or "").strip()
        if not clean:
            continue
        if clean.startswith(("『", "「")) and clean.endswith(("』", "」")):
            clean = clean[1:-1].strip()
        base = re.sub(r"[さんくん君ちゃん氏様]+$", "", clean).strip() or clean
        for candidate in (clean, base):
            if candidate and len(candidate) >= 2:
                s = s.replace(candidate, "[MASKED_USER]")
    return s

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
    for key in (
        "HABIT_PROFILE_MODEL",
        "MIDDLE_LLM_MODEL",
        "OLLAMA_MIDDLE_MODEL",
        "SUB_LLM_MODEL",
        "OLLAMA_SUB_MODEL",
        "OLLAMA_CLASSIFIER_MODEL",
        "MEMORY_EXTRACT_MODEL",
        "OLLAMA_MODEL",
    ):
        val = str(cfg(key, "") or "").strip()
        if val:
            return val
    return ""


def _looks_like_reversed_timeline_habit_reason(
    reason: str,
    user_text: str,
    assistant_text: str,
) -> bool:
    """
    習慣プロファイル更新の分析理由（reason）において、
    AI返答（assistant_text）の内容に対してユーザー（user_text）が発言・反応したかのように
    時系列・因果関係を前後逆転させているハルシネーションを検知する。
    """
    raw_reason = str(reason or "").strip()
    raw_user = str(user_text or "").strip()
    raw_assistant = str(assistant_text or "").strip()
    if not raw_reason or not raw_user or not raw_assistant:
        return False

    def _clean(s: str) -> str:
        return re.sub(r"[\s、。！？!?,.〜～…「」『』\"'“”]+", "", s).lower()

    cleaned_user = _clean(raw_user)
    cleaned_assistant = _clean(raw_assistant)
    if not cleaned_assistant:
        return False

    quote_matches = list(re.finditer(r'[「『"“\'](?P<quote>[^」』"”\']+)[」』"”\']', raw_reason))

    # パターン1: reason 内でユーザーの発言・反応として引用されているフレーズが、
    # 本来のユーザー発言ではなく assistant_text 側にのみ存在する混同・逆転ハルシネーションの検知
    # （例: 『雑菌』さんは「その熱意にちょっと圧倒されちゃいます」という文脈で共感・肯定する反応を示した）
    for m in quote_matches:
        quote = m.group("quote").strip()
        quote_clean = _clean(quote)
        if len(quote_clean) < 4:
            continue
        if quote_clean not in cleaned_assistant or quote_clean in cleaned_user:
            continue

        prefix = raw_reason[max(0, m.start() - 60):m.start()]
        suffix = raw_reason[m.end():min(len(raw_reason), m.end() + 60)]

        # 直前が「AIは」「AIが」「Botが」「アシスタントが」のようにAI自身の発話として述べられている場合は除く
        is_ai_speaking = bool(
            re.search(r'(?:AI|Bot|bot|アシスタント|こちら)(?:側)?(?:は|が|の)?\s*$', prefix, re.IGNORECASE)
            or re.search(r'(?:AI|Bot|bot|アシスタント|こちら)(?:側)?の\s*(?:発言|返答|言葉|指摘|注意|ツッコミ|冗談)\s*$', prefix, re.IGNORECASE)
        )
        if is_ai_speaking:
            continue

        # ユーザーの発言・反応・共感の文脈として引用されているか検証
        is_user_reaction_context = bool(
            re.search(
                r'^(?:という|といった)?\s*(?:文脈で|形で|ように)?\s*[^。\n]{0,25}(?:共感|肯定|同調|反応|発言|返答|回答|言及|言い|返し|ツッコ|反論|否定|受け入れ|受け止め|リアクション)',
                suffix,
            )
            or re.search(
                r'^(?:と|って)\s*(?:即座に|率直に|冗談めかして|軽く)?\s*(?:共感|肯定|同調|反応|発言|返答|回答|言及|言い|返し|ツッコ|反論|否定|受け入れ|受け止め|答えた|言った|返した|述べた)',
                suffix,
            )
        )
        if is_user_reaction_context:
            return True

    # パターン2: AI主語＋返答/発話＋「した際/したとき/した時」＋ユーザー反応（引用符の有無を問わず捕捉）
    # 例: 「AIが冗談めかした口調で返答した際、それを受け入れて同調する反応を示した」
    if re.search(
        r'(?:AI|Bot|bot|アシスタント|こちら)(?:側)?(?:が|の|は)?\s*'
        r'[^。\n]{0,80}?'
        r'(?:返答|回答|発言|応答|表現|ツッコミ|冗談|ジョーク|注意|指摘|言葉|問いかけ|リアクション|返信|からかい|煽り)'
        r'[^。\n]{0,40}?'
        r'(?:した際|したとき|した時|した上で|したのに|入れた(?:際|とき|時)|言った(?:際|とき|時))'
        r'[^。\n]{0,80}?'
        r'(?:反応|反論|反発|ツッコミ|共感|肯定|同調|受け入れ|受け止め|否定せず)',
        raw_reason,
        re.IGNORECASE,
    ):
        return True

    # パターン3: AI発言引用 ＋ 逆転接続句（に対し/返答した際/否定せず 等） ＋ ユーザー反応
    for m in quote_matches:
        quote = m.group("quote").strip()
        quote_clean = _clean(quote)
        if len(quote_clean) < 3 or len(quote_clean) > 80:
            continue

        # 今回の assistant_text にしか含まれないフレーズか検証
        if quote_clean not in cleaned_assistant or quote_clean in cleaned_user:
            continue

        prefix = raw_reason[max(0, m.start() - 50):m.start()]
        suffix = raw_reason[m.end():min(len(raw_reason), m.end() + 140)]

        # 1. AI発言であることを示す語句が前または後にあるか
        is_ai_source = bool(
            re.search(r'(?:AI|Bot|bot|アシスタント|こちら|自分)', prefix, re.IGNORECASE)
            or re.search(
                r'^(?:という|といった)?\s*(?:AI|Bot|bot|アシスタント|こちら)?(?:側)?の\s*(?:注意|発言|返答|指摘|説明|ツッコミ|言葉|問いかけ|リアクション|返信|冗談|ジョーク|からかい|煽り)?',
                suffix,
                re.IGNORECASE,
            )
            or re.search(
                r'^(?:という|といった)?\s*(?:少し|ちょっと|かなり)?\s*(?:皮肉めいた|軽い|真面目な|厳しい|強い)?\s*(?:注意|指摘|返答|発言|ツッコミ|返信|冗談|ジョーク|からかい|言葉|コメント)',
                suffix,
            )
        )

        # 2. 直後に「対し(て)?」「を受けて」「に対する」「に反論」「に反応して」「返答した際」「を否定せず」などの接続があるか（読点や長めの修飾句を許容）
        is_towards = bool(
            re.search(
                r'^(?:という|といった)?[^。\n]{0,60}(?:'
                r'に対し(?:て)?|への|を受けて|に対する|に反論|に反応(?:して)?|に反発|に反抗|にツッコ|に答え(?:て)?|に返答|に言い返|'
                r'(?:返答|回答|発言|応答|表現|ツッコミ|冗談|ジョーク)[^。\n]{0,30}(?:した際|したとき|した時|した上で|したのに)|'
                r'(?:を否定せず|を受け入れ|に共感|に肯定|に同調|反応を示し)'
                r')',
                suffix,
            )
        )
        if not is_towards:
            continue

        # 3. その接続の後にユーザーのリアクション（ユーザー発言の引用、または混同されたAI発言引用、または反応語句）があるか
        after_quotes = re.findall(r'[「『"“\']([^」』"”\']+)[」』"”\']', suffix)
        has_user_quote = any(
            len(_clean(uq)) >= 2 and _clean(uq) in cleaned_user
            for uq in after_quotes
        )
        has_confused_assistant_quote = any(
            len(_clean(uq)) >= 3 and _clean(uq) in cleaned_assistant and _clean(uq) not in cleaned_user
            for uq in after_quotes
        )

        has_reaction_word = bool(
            re.search(
                r'(?:反論|質問|発言|反応|反発|聞き返し|ツッコミ|反抗|否定|抗議|返し|言及|言い返し|問い|問いただ|問い合わ|回答|返答|リアクション|本気で|突っ込|突っかか|共感|肯定|同調|受け入れ|否定せず|挑発|冗談|続け|重ね|ぶつけ|投げかけ)',
                suffix,
            )
        )

        if (
            has_user_quote
            or has_confused_assistant_quote
            or (is_ai_source and has_reaction_word)
            or (has_reaction_word and len(quote_clean) >= 4)
        ):
            return True

    return False


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

    # ユーザー発言からURLおよび自動付与のメディア補足・リンク要約テキストを除去
    # 残りの文字列が空（または空白・記号のみ）である場合はユーザー自身の会話習慣を判定できないため早期スキップ
    cleaned_user_text = re.sub(r'https?://\S+', '', user_text)
    cleaned_user_text = re.sub(r'\[\s*(?:メディア補足|リンク要約)[^\]]*\]?', '', cleaned_user_text)
    cleaned_user_text = re.sub(r'(?:\n|\s)*(?:メディア補足|リンク要約)[\s:：][\s\S]*', '', cleaned_user_text)
    meaningful_user_chars = re.sub(r'[\s\[\]【】、。！？!?,.〜～…「」『』"\'“”\(\)（）\-_/\\#@:;~]+', '', cleaned_user_text)
    if not meaningful_user_chars:
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
    if re.match(r"^ユーザー_\d+$", base_name):
        base_name = ""
    user_label = f"『{base_name}』さん" if base_name else "対象ユーザー"

    context_text = _join_context_lines(
        context_lines,
        limit=int(cfg("HABIT_PROFILE_CONTEXT_LINES", 16) or 16),
        max_chars_per_line=int(cfg("HABIT_PROFILE_CONTEXT_MAX_CHARS_PER_LINE", cfg("MEMORY_LONG_CONTEXT_MAX_CHARS_PER_LINE", 260)) or 260),
        max_total_chars=int(cfg("HABIT_PROFILE_CONTEXT_MAX_TOTAL_CHARS", 3200) or 3200),
    )
    system_prompt = (
        "あなたはDiscord会話から、相手ごとの会話習慣プロファイルを数値化する補助AIです。\n"
        f"今回の対話相手は {user_label} です。\n"
        "出力はJSONのみです。\n"
        f"【分析の対象】『[直前の会話履歴]』（[直近文脈]）の流れを受けて、{user_label}が『[今回のユーザー発言]』（[{user_label}の発言]）をどう切り出したか・どう反応したかのみを分析してください。\n"
        "AI自身の口調や言いがかり（「被害者面しないで」等のメタ自己弁護含む）から相手の性格・習慣を逆算することを厳禁します。\n"
        "各スコアは0.0から1.0で、0.5は今回の会話からは判断しない中立値です。\n"
        "tone_casual は砕けた口調を好むほど高く、丁寧で落ち着いた口調を好むほど低くします。\n"
        "response_detail は説明を詳しくしてほしいほど高く、短く結論だけを好むほど低くします。\n"
        "search_preference は不明点や新しい話題で外部確認を望むほど高く、雑談テンポ優先ほど低くします。\n"
        "joke_receptivity は軽い冗談やツッコミを受け入れやすいほど高く、真面目な返答を好むほど低くします。\n"
        "明示的な依頼・反応・継続的な文脈から読める時だけ0.5から動かし、単なるbot側の口調を相手の好みと誤認しないでください。\n"
        f"⚠️ 判断理由（reason）には、抽象的な『ユーザー』ではなく必ず対象のサーバーニックネーム（{user_label}）を主語・対象として記述してください。"
    )
    prompt = (
        f"[対話相手]\nサーバーニックネーム: {user_label}\n\n"
        f"【分析の対象】『[直前の会話履歴]』（[直近文脈]）の流れを受けて、{user_label}が『[今回のユーザー発言]』（[{user_label}の発言]）をどう切り出したかのみを分析してください。\n"
        "AI自身の口調や言いがかり（「被害者面しないで」等）からユーザーの性格・習慣を逆算しないでください。\n\n"
        f"[候補理由]\n{candidate_reason or 'auto-saved'}\n\n"
        f"[直前の会話履歴 / 直近文脈]\n{context_text or '(なし)'}\n\n"
        f"[今回のユーザー発言 ({user_label}の発言)]\n{helpers.truncate_text(user_text, 600)}\n\n"
        f"この1ターンから、今後の{user_label}への返答に戻せる会話習慣があるか判定してください。\n"
        f"判断理由（reason）は必ず {user_label} を主語・対象として記述してください（例: 『{user_label}の発言は具体的な商品名の問い合わせであり…』）。\n"
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

    raw_reason = str(result.get("reason") or "").strip()
    if raw_reason and _looks_like_reversed_timeline_habit_reason(raw_reason, user_text, assistant_text):
        masked_reason = _mask_sensitive_log_text(raw_reason, user_id=user_id, user_names=(user_name, user_display_name, target_disp, base_name))
        masked_user = _mask_sensitive_log_text(user_text, user_id=user_id, user_names=(user_name, user_display_name, target_disp, base_name))
        masked_assistant = _mask_sensitive_log_text(assistant_text, user_id=user_id, user_names=(user_name, user_display_name, target_disp, base_name))
        log.warning(
            "update_user_habit_profile: skipped due to reversed timeline in reason (Skipped habit profile update due to reversed timeline hallucination): %s (user_text=%r, assistant_text=%r)",
            masked_reason,
            masked_user,
            masked_assistant,
        )
        return None

    # 事後正規化ガード（多重防御）: 「ユーザーの発言は」等の抽象主語を対象サーバーニックネーム・ユーザーIDタグに補正
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

    unsafe_reason = _get_unsafe_or_banter_filter_reason(user_text, memory_type="fact")
    if unsafe_reason:
        logger.info(
            "Skipped memory/training candidate due to unsafe/banter filter: reason=%s",
            unsafe_reason,
        )
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


def _extract_summary_topic_labels(summary_text: str) -> list[str]:
    clean_summary = str(summary_text or "").strip()
    if not clean_summary:
        return []
    katakana = re.findall(r"[ァ-ヶヴー]{2,}", clean_summary)
    kanji = re.findall(r"[\u4e00-\u9fff]{2,}", clean_summary)
    candidates: list[str] = []
    seen: set[str] = set()
    for w in katakana + kanji:
        w_clean = w.strip()
        if w_clean and w_clean not in seen and len(w_clean) <= 20:
            seen.add(w_clean)
            candidates.append(w_clean)
            if len(candidates) >= 5:
                break
    return candidates


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
    model = str(
        cfg("MEMORY_EXTRACT_MODEL", "")
        or cfg("MIDDLE_LLM_MODEL", "")
        or cfg("OLLAMA_MIDDLE_MODEL", "")
        or cfg("SUB_LLM_MODEL", "")
        or cfg("OLLAMA_CLASSIFIER_MODEL", "")
        or cfg("OLLAMA_MODEL", "")
    ).strip()
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

    prev_summary: str | None = None
    if store is not None and hasattr(store, "get_channel_summary"):
        try:
            prev_row = await store.get_channel_summary(persona=persona, guild_id=guild_id, channel_id=int(channel_id))
            if isinstance(prev_row, dict) and prev_row.get("summary"):
                prev_summary = str(prev_row["summary"]).strip() or None
        except Exception as e:
            log.debug("get_channel_summary failed in maybe_update_channel_summary: %r", e)

    prompt_parts = [
        f"次の会話を内部メモとして最大{max_lines}行程度で要約してください。",
        "直近の会話だけでなく、[これまでの要約] に含まれていた『誰が何をしようとしていたか（食事・予定・宣言・話題の前提）』などの具体的なエピソードも、現在の会話と矛盾しない限り1〜2文で要約内に残してください。",
        "要約を作るとき、「冗談だった」「後で否定された」「誰かの推測だった」という限定詞を落とさないこと。別々の発言の断片（数値と作品名など）を 1 つの話題として結び付けないこと。結び付きが要約元の発言に無い場合は、並列のまま残すこと。",
        "要約の長さ制限のために削る場合は、限定詞より先に具体例・繰り返しを削ること。",
    ]
    if prev_summary:
        prompt_parts.append(f"\n[これまでの要約（引き継ぎ用）]\n{prev_summary}")
    prompt_parts.append("\n[会話]\n" + "\n".join(lines))
    prompt = "\n".join(prompt_parts)

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

    topic_labels = _extract_summary_topic_labels(summary)
    set_summary_kwargs: dict[str, Any] = {
        "persona": persona,
        "guild_id": guild_id,
        "channel_id": int(channel_id),
        "summary": summary[:1000],
        "source_count": len(lines),
    }
    try:
        sig = inspect.signature(store.set_channel_summary)
        if "topic_labels" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        ):
            set_summary_kwargs["topic_labels"] = topic_labels
    except Exception:
        pass

    await store.set_channel_summary(**set_summary_kwargs)
    return True
