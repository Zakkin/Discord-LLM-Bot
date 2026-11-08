"""返信前リサーチの要否判定、検索実行、調査結果へのフィードバック記録を担当するMixin。"""
import asyncio
import contextlib
import logging
import re
import time
import unicodedata
from typing import Any, Optional

import discord

from ollama_bot.ollama_chat_.ollama_chat_helpers import _cached_other_channel_response_allowed, _get_channel_kind, _infer_feedback_type, _is_force_response_trigger, _reply_model_timeout_fallback, looks_like_feedback_text


def should_send_timeout_fallback(
    *,
    is_primary_channel: bool = False,
    is_direct: bool = False,
    force_response: bool = False,
) -> bool:
    try:
        import ollama_bot.ollama_chat_.ollama_chat_helpers as helpers
        return helpers.should_send_timeout_fallback(
            is_primary_channel=is_primary_channel,
            is_direct=is_direct,
            force_response=force_response,
        )
    except Exception:
        pass
    return bool(is_primary_channel or is_direct or force_response)
from harness.agent.prompt_parser.prompt_manager.context_helpers import (
    _check_context_flow,
    _find_last_assistant_message_text,
)
from front.discord.common.bot_identity import resolve_bot_identity
from front.discord.common.discord_helpers import author_id, channel_id, content, display_name
from front.discord.utils import strip_discord_mentions
from lib.text_utils import extract_urls
from harness.agent.prompt_parser.prompt_manager.history_helpers import safe_message_content, find_recent_bot_and_user_pair
from ollama_bot.common.ollama_helpers import extract_first_user_facing_reply, sanitize_generated_reply, truncate_text, looks_like_abnormal_assistant_reply, looks_like_parrot_reply, _normalize_compare_text, call_ollama_json, release_ollama_model, should_skip_unknown_reply, looks_like_unknown_or_incomprehension_reply, sanitize_unknown_denial_for_known_subject
from harness.agent.tool_registry.umigame_helpers import (
    _build_umigame_clear_append_message,
    _looks_like_umigame_clear,
)
from harness.agent.tool_registry.web_research import (
    build_img_thread_direct_reply,
    build_unknown_topic_fallback_reply,
    check_memory_for_topic,
    clean_img_thread_fragment,
    detect_deepdive_followup,
    detect_reply_research_rules,
    is_plausible_topic_subject,
    research_dispatch,
)
from ollama_bot.ollama_chat_.ollama_chat_texts import build_reply_research_decider_prompt, build_reply_research_decider_system_prompt, build_research_grounded_reply_prompt
from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_int, cfg_float, cfg_primary_channel_id, cfg_persona_namespace
from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime, ReplyModelTimeoutError

log = logging.getLogger("ollama_bot.ollama_chat")

REPLY_RESEARCH_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "needs_research": {"type": "boolean"},
        "mode": {"type": "string"},
        "recency_flag": {"type": "boolean"},
        "unknown_term_flag": {"type": "boolean"},
        "source_request": {"type": "boolean"},
        "reason": {"type": "string"},
        "search_query": {"type": "string"},
        "provisional_reply": {"type": "string"},
    },
    "required": [
        "needs_research",
        "mode",
        "recency_flag",
        "unknown_term_flag",
        "source_request",
        "reason",
        "search_query",
        "provisional_reply",
    ],
    "additionalProperties": False,
}

_TOPIC_OPINION_EVASION_LACK_PATTERN = re.compile(
    r"(?:詳しく|しら|知ら|分から|わから|やったこと)(?:な|無)くて|"
    r"(?:詳しく|しら|知ら|分から|わから|やったこと)(?:な|無)い(?:から|ので|んだ|んです|んす|からさ)?|"
    r"全然(?:詳しく|知ら|分から|わから)|"
    r"さっぱり(?:詳しく|知ら|分から|わから)",
    re.IGNORECASE,
)
_TOPIC_OPINION_EVASION_APOLOGY_PATTERN = re.compile(
    r"(?:ごめん|すみません|すいません|申し訳|許して|"
    r"教えられ|答えられ)(?:ない|へん|ません)?|"
    r"わからない|わかんない|知らな(?:い|ん)|お役に立て",
    re.IGNORECASE,
)


def _extract_speaker_from_context_line(line: str) -> str:
    line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
    if ":" in line_no_ts:
        speaker_part = line_no_ts.split(":", 1)[0].strip()
        return re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
    return ""


def _looks_like_topic_opinion_evasion_reply(reply_text: str) -> bool:
    text = str(reply_text or "").strip()
    if not text:
        return False
    if not _TOPIC_OPINION_EVASION_LACK_PATTERN.search(text):
        return False
    return bool(_TOPIC_OPINION_EVASION_APOLOGY_PATTERN.search(text))


def _is_abnormal_or_abusive_topic(topic: str, user_text: str = "") -> bool:
    t = str(topic or "").strip()
    u = str(user_text or "").strip()
    if not t:
        return True
    if not is_plausible_topic_subject(t):
        return True
    if looks_like_feedback_text(t) or looks_like_feedback_text(u):
        return True
    if re.search(r"[!！?？#@$%^&*()_+=\[\]{}|\\:;\"'<>,./~`¥-]{2,}", t):
        return True
    return False


def _build_topic_opinion_retry_prompt(base_prompt: str) -> str:
    return (
        f"{base_prompt}\n\n"
        "[回答の再生成指示（不知・謝罪逃げの禁止）]\n"
        "直前の回答は『詳しくないからごめんなさい』『わからない』と不知を理由に回答を放棄・謝罪してしまっていました。\n"
        "あなた自身に体験や詳しい知識がなくても全く問題ありません。\n"
        "外部調査結果にある世間の評判やそれぞれの特徴・面白さ（演出の楽しさ、予想やレースの迫力、ゲーム性の違いなど）を踏まえて、"
        "『ネットの評判だと〜らしいよ』『それぞれ〜なところが人気みたい』と客観的な評判を紹介する形で、相手の質問に必ず答えてください。\n"
        "謝罪や『詳しくないからわからない』という回答放棄は絶対に禁止です。\n"
        "設定されたキャラクターの口調を維持しつつ、セリフ本文のみを出力してください。"
    )


_RESEARCH_DUMP_QUESTION_PATTERN = re.compile(
    r"[?？]|"
    r"(?:何|なに|どう|どれ|どこ|いつ|誰|だれ|どのくらい|いくら|何故|なぜ|なんで|どうして)|"
    r"(?:教え(?:て|てください|てほしい|てくれ)|知っ(?:てる|てます|てたら)|kwsk)|"
    r"(?:(?:ですか|ますか|のか|かい|のかな|んだっけ|でしょうか)[\s。!！]*$)",
    re.IGNORECASE,
)

_RESEARCH_DUMP_REACTION_PATTERN = re.compile(
    r"(?:"
    # 感想・共感・所感
    r"美味し(?:かった|い|そう)|おいし(?:かった|い|そう)|"
    r"(?:よかっ|良かっ)(?:た|ですね|たね|たり)|"
    r"(?:すご|凄)(?:い|かった|そう)|"
    r"(?:楽し|たのし)(?:かった|い|そう)|"
    r"(?:いい|良|よ)い(?:ね|な|ですね|ですよ)|"
    r"(?:うらやまし|羨まし)|"
    # 体験・行動の受容・相槌（〜したんだ、〜したんですね、〜したんですか等）
    r"(?:飲|食|買|行|見|聞|読|遊|使|試|作|乗|持)(?:んだ|べた|った|った|た|いた|んだ|んだ|った|した|った|った)(?:ん|の|んです|んだ|のか|ですか|ね|な|よ)?|"
    r"(?:した|だった|られた)(?:ん|の|んです|んだ|のか|ですか|ね|な|よ)(?:か|ね|！|!|？|\?|$)|"
    r"(?:たん|たんす|たの|たんで|だったん|でした)(?:ね|か|よ|さ)|"
    r"(?:ですね|ますね|でしたね|ましたね)|"
    # 相槌から始まる文
    r"(?:^|[\n。！？!?])\s*(?:へえ|へー|おお|わあ|わー|えっ|そうなん|そうなんだ|なるほど)"
    r")",
    re.IGNORECASE,
)


def _looks_like_raw_research_dump(
    reply_text: str,
    research_summary: str,
    user_text: str = "",
) -> bool:
    """外部調査サマリーの文章をそのまま丸写し・一方的講釈し、対話相手へのリアクションを欠いているか判定する。"""
    reply = str(reply_text or "").strip()
    summary = str(research_summary or "").strip()
    if not reply or not summary:
        return False

    # 1. ユーザー発言が質問（? 等）である場合は、知識・回答提供が期待されるため除外
    u_text = str(user_text or "").strip()
    if u_text and _RESEARCH_DUMP_QUESTION_PATTERN.search(u_text):
        return False

    # 2. 返答に相手へのリアクション（共感・受容・体験への言及）があるか確認
    if _RESEARCH_DUMP_REACTION_PATTERN.search(reply):
        return False

    # 3. 調査サマリーからの丸写し（20文字以上の連続一致、あるいは複数の文の一致）をチェック
    def _norm(t: str) -> str:
        return re.sub(r"[\s\u3000\t\r\n]+", "", unicodedata.normalize("NFKC", t))

    norm_reply = _norm(reply)
    if not norm_reply:
        return False

    sentences = [s.strip() for s in re.split(r"[。\n\r！？!?]+", summary) if s.strip()]
    has_long_phrase_match = False
    match_count = 0

    for sent in sentences:
        core = _norm(sent)
        if not core:
            continue
        # 日本語の実質文字（漢字・ひらがな・カタカナ）を含むかチェック
        if not re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", core):
            continue

        matched_this_sent = False
        if len(core) >= 20:
            for i in range(len(core) - 19):
                sub = core[i : i + 20]
                if sub in norm_reply:
                    has_long_phrase_match = True
                    matched_this_sent = True
                    break
        elif len(core) >= 12:
            if core in norm_reply:
                matched_this_sent = True

        if matched_this_sent:
            match_count += 1

    return bool(has_long_phrase_match or match_count >= 2)


def _build_research_conversation_retry_prompt(base_prompt: str, user_text: str = "") -> str:
    return (
        f"{base_prompt}\n\n"
        "[回答の再生成指示（調査結果の丸写し・一方的講釈の禁止と対話校正）]\n"
        "直前の回答はインターネットで調べた解説文や要約をそのまま一方的に読み上げてしまっており、相手のメッセージ（体験や感想）に対する会話・返信として成立していませんでした。\n"
        "相手の発言内容（飲んだこと、食べたこと、感想など）に対するリアクションや共感から会話を始めてください。\n"
        "調べた知識（リニューアルや新機能など）は相手の話題に関連する要点を1点だけ自然に触れる程度にし、歴史や製法・企業戦略などの過剰な講釈は絶対に省いてください。\n"
        "1〜3文程度の自然なチャットの会話（返信）として文章を校正し直して出力してください。\n"
        "設定されたキャラクターの口調を維持しつつ、セリフ本文のみを出力してください。"
    )


_META_SEARCH_ACTION_RE = re.compile(
    r"(?:調べてみた|検索してみた|確認してみた|探してみた|ググってみた|"
    r"調べてみたんですが|調べてみたんですけど|検索してみたんですが|検索してみたんですけど)",
    re.IGNORECASE,
)

_META_SEARCH_FAILURE_OR_NOISE_RE = re.compile(
    r"(?:定義までは確認でき|意味までは確認でき|詳細までは確認でき|確認できなかった|"
    r"ニュースばかりが並|記事ばかりが並|パーティーとか[^\n。！？!?]{0,20}ばかり|"
    r"特定の作品のネタか|別の固有名詞のことですか|何のことか確認でき|"
    r"どういう意味か確認でき|情報が見つからな|検索しても出てこ)",
    re.IGNORECASE,
)


def _looks_like_meta_search_failure_report(
    reply_text: str,
    user_text: str = "",
) -> bool:
    """非質問の短文発言・リアクションに対して『調べてみたけど定義が確認できなかった』『別ニュースばかり並んだ』等のメタ検索失敗レポートを返しているか判定する。"""
    reply = str(reply_text or "").strip()
    if not reply:
        return False

    u_text = str(user_text or "").strip()
    if u_text and _RESEARCH_DUMP_QUESTION_PATTERN.search(u_text):
        return False
    if u_text and re.search(r"(?:とは|って何|ってなに|について|意味|調べ|検索)", u_text):
        return False

    if _META_SEARCH_FAILURE_OR_NOISE_RE.search(reply):
        return True
    if _META_SEARCH_ACTION_RE.search(reply) and re.search(
        r"(?:分からな|わからな|見つからな|確認できな|追いつかな|出てこな)",
        reply,
    ):
        return True
    return False


def _build_contextual_reaction_retry_prompt(
    base_prompt: str,
    *,
    user_text: str = "",
    context_lines: list[str] | None = None,
    intent_info: dict[str, Any] | None = None,
) -> str:
    payload = dict(intent_info or {})
    raw_delib = payload.get("deliberation_info")
    delib: dict[str, Any] = raw_delib if isinstance(raw_delib, dict) else {}
    topic_origin = str(payload.get("topic_origin") or delib.get("topic_origin") or "").strip()
    avoid = str(payload.get("avoid") or delib.get("avoid") or "").strip()

    ctx_block = ""
    if context_lines:
        recent = [str(line).strip() for line in context_lines[-6:] if str(line).strip()]
        if recent and "【直前の会話文脈】" not in base_prompt:
            ctx_block = "\n【直前の会話文脈】\n" + "\n".join(recent) + "\n"

    origin_hint = f"\n- 直前の話題の背景: {topic_origin}" if topic_origin else ""
    avoid_hint = f"\n- 注意点: {avoid}" if avoid else ""

    return (
        f"{base_prompt}{ctx_block}\n\n"
        "[回答の再生成指示（辞書引き失敗レポート・検索ノイズ言及の禁止と文脈リアクションへの校正）]\n"
        f"直前の回答は、相手の発言（『{user_text}』）を文字通りの未知の専門用語だと勘違いし、"
        "『調べてみたんですけど定義が確認できなかった』『無関係なニュースばかり出てきた』『特定の作品のネタですか？』という不自然な検索結果レポートになってしまっていました。\n"
        "相手は言葉の定義を質問しているのではなく、直前の会話の流れに対して一言でツッコミ・比喩・リアクション（例: 直前の『絵柄泥棒』やLoRA相談の流れを『泥棒会議』と呼ぶ等）をしています。"
        f"{origin_hint}{avoid_hint}\n"
        "検索結果に出てきた無関係な固有名詞（チョッパーや別アニメのニュース等）や『調べてみたけど分からなかった』という報告は【完全に破棄】し、"
        "直前の会話の流れと相手のツッコミ（ネーミング）に対する等身大のリアクション・ツッコミ返しとして、1〜2文（50〜110文字程度）の自然な返信に書き直してください。\n"
        "設定されたキャラクターの口調を維持しつつ、セリフ本文のみを出力してください。"
    )


_CONSULTATION_SELF_PROJECTION_RE = re.compile(
    r"(?:私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち)も[^\n。！？!?]{1,35}?"
    r"(?:困ったこと(?:が)?ある|探してて困った|悩んだこと(?:が)?ある|途方に暮れた|"
    r"分からなくて困った|わからなくて困った|経験(?:が)?あるんですよ|同じこと思った)",
    re.IGNORECASE,
)

_CONSULTATION_SOLUTION_MARKER_RE = re.compile(
    r"(?:クロール|スクレイピング|チェック|確認|探すと|探ったら|残って|放置|ツール|スクリプト|"
    r"ソフト|アプリ|拡張機能|通知|差分|巡回|代替|方法|やり方|設定|使うと|すれば|してみて|"
    r"おすすめ|候補|しか無い|しかない|手もあります|やり方もあります)",
    re.IGNORECASE,
)


def _looks_like_consultation_evasion_or_lecture(
    reply_text: str,
    research_summary: str,
    user_text: str = "",
) -> bool:
    """困りごと・意見募集への返信が『私も困ったことがある』等の自己投影逃げ、または長文解説・検索結果丸写しになっていないか判定する。"""
    reply = str(reply_text or "").strip()
    if not reply:
        return False

    # 1. 自己投影ミラーリング（「私も〜困ったことあるんですよ」等）で、具体的な解決策の提案がない場合
    proj_re = _CONSULTATION_SELF_PROJECTION_RE
    try:
        from lib.config_utils import cfg
        fp = str(cfg("BOT_FIRST_PERSON", "") or cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if fp:
            escaped_fp = re.escape(fp)
            proj_re = re.compile(
                rf"(?:{escaped_fp}|私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち)も[^\n。！？!?]{{1,35}}?"
                r"(?:困ったこと(?:が)?ある|探してて困った|悩んだこと(?:が)?ある|途方に暮れた|"
                r"分からなくて困った|わからなくて困った|経験(?:が)?あるんですよ|同じこと思った)",
                re.IGNORECASE,
            )
    except Exception:
        pass

    if proj_re.search(reply):
        sentences = [s.strip() for s in re.split(r"[。\n\r！？!?]+", reply) if s.strip()]
        non_sympathy = " ".join(s for s in sentences if not proj_re.search(s))
        if not _CONSULTATION_SOLUTION_MARKER_RE.search(non_sympathy):
            return True

    # 2. 長文解説化（240文字超）または調査サマリーの25文字以上の丸写し
    if len(reply) > 240:
        return True

    summary = str(research_summary or "").strip()
    if summary:
        def _norm(t: str) -> str:
            return re.sub(r"[\s\u3000\t\r\n]+", "", unicodedata.normalize("NFKC", t))

        norm_reply = _norm(reply)
        for sent in [s.strip() for s in re.split(r"[。\n\r！？!?]+", summary) if s.strip()]:
            core = _norm(sent)
            if len(core) >= 25:
                for i in range(len(core) - 24):
                    if core[i : i + 25] in norm_reply:
                        return True

    return False


def _build_consultation_solution_retry_prompt(base_prompt: str, user_text: str = "") -> str:
    return (
        f"{base_prompt}\n\n"
        "[回答の再生成指示（困りごと・意見募集に対する簡潔な解決策の提示）]\n"
        "直前の回答は『自分も困ったことがある』という表面的な共感だけで解決策が抜けていたか、あるいは検索結果の丸写し・長文解説になっていました。\n"
        "相手は困っていて意見や解決につながる方法・代替手段を求めています。\n"
        "外部調査結果から、相手の困りごとの解決に繋がる具体的な方法・代替案・コツ（例: 昔のフィードが残っていないか探す、定時クロールで更新チェックする等）を1〜2点だけ選び、"
        "検索結果のコピペや長文解説にはせず、キャラクターの自然な口調で1〜3文（60〜160文字程度）で簡潔に教えてあげてください。\n"
        "セリフ本文のみを出力してください。"
    )


from typing import TYPE_CHECKING

from front.discord.utils import strip_discord_mentions
from lib.text_utils import extract_urls
from front.discord.common.bot_identity import resolve_bot_identity
from harness.agent.prompt_parser.prompt_manager.research_prompts import (
    append_research_citations as _append_research_citations_fn,
    build_research_context_text,
    build_research_reply_base_prompt,
    build_research_break_base_prompt_text,
)

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _ResearchEventBase = OllamaChatProtocol
else:
    _ResearchEventBase = object


def _get_img2chan_helper() -> Any:
    """ふたば/img2chan用ドメインヘルパーを取得する。"""
    try:
        from front.registry import load_domain_helper
        return load_domain_helper("hutaba")
    except Exception:
        try:
            import front.hutaba.client.img2chan as _helper_hutaba
            return _helper_hutaba
        except Exception:
            try:
                import harness.agent.tool_registry.mcp.chrome.img2chan as _helper_chrome
                return _helper_chrome
            except Exception:
                return None


def _extract_first_hutaba_thread_url(text: str, *, helper: Any = None) -> str:
    """テキストから先頭のふたばスレッドURL（img/may）を1件のみ抽出する。"""
    if not text:
        return ""
    if helper is None:
        helper = _get_img2chan_helper()
    if helper is not None and hasattr(helper, "_extract_img_thread_urls_full"):
        try:
            urls = helper._extract_img_thread_urls_full(text)
            if urls and isinstance(urls, (list, tuple)):
                for u in urls:
                    u_str = str(u).strip()
                    if u_str:
                        return u_str
        except Exception:
            pass
    m = re.search(r"https?://(?:img|may)\.2chan\.net/b/res/\d+\.htm", text, re.IGNORECASE)
    return m.group(0) if m else ""


class _ResearchEventMixin(_ResearchEventBase):
    def _get_img2chan_helper(self) -> Any:
        return _get_img2chan_helper()
    def _build_research_context_text(self, runtime: MessageRuntime) -> str:
        clean_lines = [
            line for line in (runtime.context_lines or [])
            if not (line.startswith("System:") or "内心のメモ:" in line or line.startswith("system:"))
        ]
        return build_research_context_text(
            context_lines=clean_lines,
            effective_user_text=runtime.effective_user_text or "",
            original_user_text=runtime.original_user_text or "",
        )

    def _build_research_reply_base_prompt(
        self,
        runtime: MessageRuntime,
        *,
        message: discord.Message | None = None,
        last_assistant_text: str = "",
        intent_info: dict[str, Any] | None = None,
        topic_break: bool = False,
    ) -> str:
        raw_user_text = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()
        user_text_for_prompt = strip_discord_mentions(raw_user_text).strip() or raw_user_text

        bot_ident = resolve_bot_identity(bot=getattr(self, "bot", None), message=message)
        primary_name = bot_ident.primary_name
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        target_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
            or "ユーザー"
        )

        eff_intent_info = dict(intent_info or {})
        if isinstance(getattr(runtime, "working_context", None), dict):
            wc_topic = str(runtime.working_context.get("current_thread_topic") or "").strip()
            if wc_topic and wc_topic not in ("雑談", "general", "不明") and not eff_intent_info.get("topic_origin"):
                eff_intent_info["topic_origin"] = wc_topic

        ctx_lines = list(runtime.context_lines) if getattr(runtime, "context_lines", None) else None
        try:
            return build_research_reply_base_prompt(
                user_text_for_prompt=user_text_for_prompt,
                target_name=target_name,
                bot_primary_name=primary_name,
                bot_user_id=bot_user_id,
                topic_break=topic_break,
                last_assistant_text=last_assistant_text,
                intent_info=eff_intent_info,
                media_prompt_parts=list(runtime.media.prompt_parts) if runtime.media.prompt_parts else None,
                context_lines=ctx_lines,
            )
        except TypeError:
            return build_research_reply_base_prompt(
                user_text_for_prompt=user_text_for_prompt,
                target_name=target_name,
                bot_primary_name=primary_name,
                bot_user_id=bot_user_id,
                topic_break=topic_break,
                last_assistant_text=last_assistant_text,
                intent_info=eff_intent_info,
                media_prompt_parts=list(runtime.media.prompt_parts) if runtime.media.prompt_parts else None,
            )

    async def _build_research_break_base_prompt(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        incoming_emotion_scores: dict[str, float] | None = None,
    ) -> tuple[str, str, dict[str, Any]]:
        raw_user_text = (
            runtime.effective_user_text
            or runtime.original_user_text
            or runtime.media.analysis_text
            or "（本文なし）"
        ).strip()
        user_text_for_prompt = strip_discord_mentions(raw_user_text).strip() or raw_user_text

        bot_ident = resolve_bot_identity(bot=getattr(self, "bot", None), message=message)
        primary_name = bot_ident.primary_name
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        target_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
            or "ユーザー"
        )

        prompt = build_research_break_base_prompt_text(
            user_text_for_prompt=user_text_for_prompt,
            target_name=target_name,
            bot_primary_name=primary_name,
            bot_user_id=bot_user_id,
        )

        core_beliefs: list[str] = []
        get_core_beliefs = getattr(self, "_get_core_beliefs_for_prompt", None)
        if callable(get_core_beliefs):
            with contextlib.suppress(Exception):
                core_beliefs = await get_core_beliefs(message)
        if core_beliefs:
            from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
            target_uid = author_id(message)
            target_aliases: list[str] = []
            if target_uid is not None:
                try:
                    from harness.agent.memory_manager.user_identity_resolver import get_user_aliases_from_store
                    target_aliases = await get_user_aliases_from_store(
                        target_uid,
                        guild=getattr(message, "guild", None),
                        memory_store=getattr(self, "memory_store", None),
                    )
                except Exception:
                    pass
            belief_lines = format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=target_name,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
            if belief_lines:
                prompt = "\n".join([prompt, *belief_lines])

        social_guidance = ""
        get_social_guidance = getattr(self, "_get_current_reply_social_guidance", None)
        if callable(get_social_guidance):
            with contextlib.suppress(Exception):
                social_guidance = await get_social_guidance(runtime, incoming_emotion_scores)
        prepend_social_guidance = getattr(self, "_prepend_social_guidance_to_prompt", None)
        if social_guidance and callable(prepend_social_guidance):
            prompt = prepend_social_guidance(prompt, social_guidance)

        intent_info = {
            "intent": "request",
            "target_is_ai": False,
            "should_clarify": False,
            "spontaneous_source": "",
        }
        return prompt, "", intent_info

    def _append_research_citations(self, reply: str, research_result: dict[str, Any] | None) -> str:
        return _append_research_citations_fn(reply, research_result)

    def _disabled_research_decision(self) -> dict[str, Any]:
        return {
            "needs_research": False,
            "mode": "simple_search",
            "query": "",
            "reason": "",
            "provisional_reply": "",
            "recency_flag": False,
            "unknown_term_flag": False,
            "source_request": False,
            "url_present": False,
            "x_timeline_flag": False,
            "x_search_flag": False,
            "conversational_followup": False,
            "rule_reasons": [],
        }

    def _research_decision_timeout_sec(self) -> float:
        return max(cfg_float("REPLY_RESEARCH_DECISION_TIMEOUT_SEC", 10.0), 1.0)

    def _default_research_provisional_text(self, decision: dict[str, Any] | None = None) -> str:
        if not cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False):
            return ""
        payload = dict(decision or {})
        if bool(payload.get("url_present")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_URL_REPLY", "その先を少し見てくる。") or "その先を少し見てくる。"
            ).strip()
        if bool(payload.get("x_timeline_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_X_TIMELINE_REPLY", "今のXを少し見てくる。")
                or "今のXを少し見てくる。"
            ).strip()
        if bool(payload.get("recency_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_RECENCY_REPLY", "今の情報を少し見てくる。")
                or "今の情報を少し見てくる。"
            ).strip()
        if bool(payload.get("unknown_term_flag")):
            return str(
                cfg("REPLY_RESEARCH_PROVISIONAL_UNKNOWN_REPLY", "そこは少し確認してくる。")
                or "そこは少し確認してくる。"
            ).strip()
        return str(cfg("REPLY_RESEARCH_PROVISIONAL_REPLY", "少し調べてから返す。") or "少し調べてから返す。").strip()

    def _finalize_research_provisional_text(
        self,
        text: object,
        *,
        user_text: str,
        decision: dict[str, Any] | None = None,
    ) -> str:
        raw_text = str(text or "").strip()
        if raw_text.upper() == "SKIP":
            return "SKIP"

        cleaned = truncate_text(raw_text, max(cfg_int("REPLY_PROVISIONAL_MAX_CHARS", 40), 10))
        if not cleaned:
            return self._default_research_provisional_text(decision)
        if looks_like_abnormal_assistant_reply(cleaned):
            return self._default_research_provisional_text(decision)
        if looks_like_parrot_reply(user_text, cleaned):
            return self._default_research_provisional_text(decision)
        return cleaned

    def _choose_research_provisional_text(
        self,
        *,
        model_result: dict[str, Any],
        base_decision: dict[str, Any],
        user_text: str,
    ) -> str:
        if not cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False):
            return ""
        model_text = str(model_result.get("provisional_reply") or "").strip()
        if model_text.upper() == "SKIP":
            candidate = "SKIP"
        elif cfg_bool("REPLY_RESEARCH_USE_MODEL_PROVISIONAL", False) and model_text:
            candidate = model_text
        else:
            candidate = str(base_decision.get("provisional_reply") or "")
        return self._finalize_research_provisional_text(
            candidate,
            user_text=user_text,
            decision=base_decision,
        )

    def _normalize_research_mode(self, mode: object, *, fallback: str = "simple_search") -> str:
        normalized = str(mode or "").strip().lower()
        if normalized in {"simple_search", "browser_search", "browser_read_url", "x_timeline", "x_search", "img_top5", "img_thread", "image_search"}:
            return normalized
        return fallback

    def _should_use_research_reply(self, decision: dict[str, Any] | None) -> bool:
        if not isinstance(decision, dict):
            return False
        if bool(decision.get("suppressed_by_llm")):
            return False
        return bool(decision.get("needs_research"))

    def _research_summary_looks_like_login_wall(self, summary: str) -> bool:
        lowered = str(summary or "").lower()
        return any(
            phrase in lowered
            for phrase in (
                "don't miss what's happening",
                "don’t miss what’s happening",
                "people on x are the first to know",
                "something went wrong, but don't fret",
                "something went wrong, but don’t fret",
                "some privacy related extensions may cause issues on x.com",
            )
        )

    def _is_research_result_insufficient(self, research_result: dict[str, Any] | None) -> bool:
        if not isinstance(research_result, dict):
            return True
        if str(research_result.get("error") or "").strip():
            return True
        summary = str(research_result.get("summary") or "").strip()
        if not summary or summary in {"検索結果が見つかりませんでした。", "URLの読み取りに失敗しました。"}:
            return True
        if self._research_summary_looks_like_login_wall(summary):
            return True
        if not list(research_result.get("sources") or []) and summary.startswith("検索がタイムアウトしました。"):
            return True
        return False

    def _build_research_failure_reply(
        self,
        decision: dict[str, Any] | None,
        research_result: dict[str, Any] | None,
    ) -> str:
        payload = dict(decision or {})
        if bool(payload.get("topic_opinion_flag")):
            topic = str(payload.get("target_subject") or payload.get("query") or "").strip()
            user_text = str(payload.get("original_user_text") or "").strip()
            if not _is_abnormal_or_abusive_topic(topic, user_text):
                persona = cfg_persona_namespace(custom_cfg=cfg)
                return build_unknown_topic_fallback_reply(
                    user_text,
                    target_subject=topic,
                    persona=persona,
                )

        result = dict(research_result or {})
        reason = str(result.get("error") or "").strip()
        summary = str(result.get("summary") or "").strip()
        if not reason and (
            summary in {"検索結果が見つかりませんでした。", "URLの読み取りに失敗しました。"}
            or "タイムアウト" in summary
        ):
            reason = summary
        if not reason:
            reason = "十分な情報が取れなかった"

        action = "確認"
        if bool(payload.get("url_present")):
            action = "URL先まで確認"
        elif bool(payload.get("x_search_flag")) or payload.get("mode") == "x_search":
            action = "X（Twitter）の投稿を確認"
        elif bool(payload.get("x_timeline_flag")):
            action = "Xのタイムラインを確認"
        elif bool(payload.get("recency_flag")):
            action = "今の情報を確認"
        elif bool(payload.get("unknown_term_flag")):
            action = "その語を確認"

        reason_for_reply = reason.rstrip("。.!！？")
        template = str(
            cfg(
                "REPLY_RESEARCH_FAILURE_TEMPLATE",
                "{action}したが、十分な情報が取れなかった。理由は {reason}。",
            )
            or "{action}したが、十分な情報が取れなかった。理由は {reason}。"
        )
        try:
            reply = template.format(action=action, reason=reason_for_reply)
        except Exception:
            reply = f"{action}したが、十分な情報が取れなかった。理由は {reason_for_reply}。"
        return truncate_text(reply, 150)

    def _research_reply_num_predict(self) -> int | None:
        base = cfg_int("OLLAMA_REPLY_NUM_PREDICT", 128)
        reply_num_predict = getattr(self, "_reply_num_predict", None)
        if callable(reply_num_predict):
            with contextlib.suppress(Exception):
                resolved = reply_num_predict()
                if resolved is not None:
                    base = int(resolved)
        configured = cfg_int("REPLY_RESEARCH_NUM_PREDICT", 384)
        value = max(base, configured)
        return value if value > 0 else None

    def _is_configured_failure_reply(self, reply: str) -> bool:
        normalized = _normalize_compare_text(reply)
        if not normalized:
            return True
        fallback_values = (
            cfg("OLLAMA_EMPTY_REPLY_FALLBACK", "今のはうまく返せなかった。もう一回言ってくれ。"),
            cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。"),
        )
        return any(normalized == _normalize_compare_text(str(value or "")) for value in fallback_values)

    def _clean_img_thread_fragment(self, text: object, *, max_chars: int = 80) -> str:
        return clean_img_thread_fragment(text, max_chars=max_chars)

    def _build_img_thread_direct_reply(self, research_result: dict[str, Any] | None) -> str:
        return build_img_thread_direct_reply(research_result)

    async def should_research_for_reply(
        self,
        runtime: MessageRuntime,
        *,
        message: discord.Message | None = None,
        force_response: bool = False,
    ) -> dict[str, Any]:
        if not cfg_bool("REPLY_RESEARCH_ENABLED", True):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        if getattr(runtime, "is_umigame_reply", False) or getattr(runtime, "is_twenty_doors_reply", False):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        if (
            message is not None
            and not force_response
            and _get_channel_kind(message) == "other"
            and not _cached_other_channel_response_allowed(message, cog=self)
        ):
            decision = self._disabled_research_decision()
            runtime.reply_research_decision = decision
            return decision

        cid = channel_id(message) if message is not None else getattr(runtime, "cid", None)
        channel_cache = getattr(self, "_channel_research_cache", {})
        last_research_cache = channel_cache.get(cid) if isinstance(channel_cache, dict) and cid is not None else None

        raw_intent = getattr(runtime, "intent", None)
        if not raw_intent and isinstance(getattr(runtime, "working_context", None), dict):
            raw_intent = runtime.working_context.get("intent")
            if not raw_intent:
                wc_info = runtime.working_context.get("intent_info")
                if isinstance(wc_info, dict):
                    raw_intent = wc_info.get("intent")
        if not raw_intent:
            runtime_intent_info = getattr(runtime, "intent_info", None)
            if isinstance(runtime_intent_info, dict):
                raw_intent = runtime_intent_info.get("intent")

        intent_str = str(raw_intent or "").strip()
        is_feedback_or_abusive = (
            looks_like_feedback_text(runtime.original_user_text)
            or intent_str in ("暴言", "苦情", "煽り")
        )

        deepdive_info = detect_deepdive_followup(runtime.original_user_text, last_research_cache)
        if deepdive_info.get("is_deepdive") and not is_feedback_or_abusive:
            deepdive_mode = str(deepdive_info.get("deepdive_mode") or "").strip()
            target_url = str(deepdive_info.get("target_url") or "").strip()
            mode = "img_thread" if deepdive_mode == "img_thread" else "x_timeline"
            if mode == "img_thread":
                # 複数URLが含まれる場合は先頭1件のみを抽出
                first_url = _extract_first_hutaba_thread_url(
                    f"{target_url} {runtime.original_user_text}",
                    helper=self._get_img2chan_helper(),
                )
                if first_url:
                    target_url = first_url
            decision = {
                "needs_research": True,
                "mode": mode,
                "query": target_url or str((last_research_cache or {}).get("query") or ""),
                "reason": truncate_text(str(deepdive_info.get("reason") or "直前調査の深掘り"), 40),
                "provisional_reply": "",
                "recency_flag": mode == "x_timeline",
                "unknown_term_flag": False,
                "source_request": False,
                "url_present": bool(target_url),
                "x_timeline_flag": mode == "x_timeline",
                "rule_reasons": ["deepdive_followup"],
            }
            runtime.reply_research_decision = decision
            return decision

        context_text = self._build_research_context_text(runtime)
        rule_info = detect_reply_research_rules(runtime.original_user_text, context_text)

        # may.2chan.net 判定連携および複数URL指定時の先頭1件限定処理 (BAN-172, BAN-179)
        # detect_reply_research_rules で img_thread が検知された場合、またはテキスト内にふたばスレッドURLがある場合、
        # スレッドURLリストの先頭1件を確実に query に設定し、img_thread モードを確定させる
        hutaba_helper = self._get_img2chan_helper()
        first_thread_url = _extract_first_hutaba_thread_url(
            f"{rule_info.get('query') or ''} {runtime.original_user_text or ''}",
            helper=hutaba_helper,
        )
        is_thread_url = bool(first_thread_url)
        if not is_thread_url and hutaba_helper is not None and hasattr(hutaba_helper, "is_img_thread_url"):
            for word in (runtime.original_user_text or "").split():
                if hutaba_helper.is_img_thread_url(word):
                    first_thread_url = word
                    is_thread_url = True
                    break

        if (
            rule_info.get("mode") == "img_thread"
            or "img_thread" in (rule_info.get("rule_reasons") or [])
            or is_thread_url
        ) and first_thread_url:
            rule_info["mode"] = "img_thread"
            rule_info["needs_research"] = True
            rule_info["query"] = first_thread_url
            rule_info["url_present"] = True
            reasons = list(rule_info.get("rule_reasons") or [])
            if "img_thread" not in reasons:
                reasons.append("img_thread")
                rule_info["rule_reasons"] = reasons

        if is_feedback_or_abusive:
            rule_info["topic_opinion_flag"] = False
            rule_info["consultation_flag"] = False
            rule_info["needs_research"] = False
            rule_info["should_consult_model"] = False
            rule_info["unknown_term_flag"] = False

        # ステップ0: サーバーメンバー（ユーザー名）の最優先照合（User-First Resolution）
        # candidate_termだけでなく、target_subject, named_terms全体、および発言本文から抽出された
        # 固有名詞候補をすべてGuildメンバー名（ニックネーム・ユーザー名）およびDBプロファイルと走査照合する。
        # いずれかの単語が実在メンバーと一致した場合、外部Web調査を即座にブロックする。
        # （BAN-21, BAN-61, BAN-102, BAN-131）
        gid = getattr(getattr(message, "guild", None), "id", None)
        memory_store = getattr(self, "memory_store", None)

        candidate_terms: list[str] = []
        target_subj = str(rule_info.get("target_subject") or "").strip()
        if target_subj and target_subj not in candidate_terms:
            candidate_terms.append(target_subj)
        cand_term = str(rule_info.get("candidate_term") or "").strip()
        if cand_term and cand_term not in candidate_terms:
            candidate_terms.append(cand_term)
        named_terms_list = rule_info.get("named_terms")
        if isinstance(named_terms_list, (list, tuple, set)):
            for nt in named_terms_list:
                nt_s = str(nt or "").strip()
                if nt_s and nt_s not in candidate_terms:
                    candidate_terms.append(nt_s)

        user_raw = str(runtime.original_user_text or "").strip()
        if user_raw:
            try:
                from harness.agent.tool_registry.web_research.helpers import extract_named_term_candidates
                for ext in extract_named_term_candidates(user_raw):
                    ext_s = str(ext or "").strip()
                    if ext_s and ext_s not in candidate_terms:
                        candidate_terms.append(ext_s)
            except Exception as e:
                log.debug("failed to extract named term candidates from user text: %r", e)

        matched_member = None
        matched_term = ""

        if gid is not None and candidate_terms:
            try:
                profiles = []
                if memory_store and hasattr(memory_store, "list_user_profiles_for_guild"):
                    profiles = await memory_store.list_user_profiles_for_guild(guild_id=int(gid), limit=1000)
                from harness.agent.prompt_parser.prompt_manager.user_profile_logic import (
                    find_matching_guild_member,
                    merge_guild_and_db_profiles,
                )
                profiles = merge_guild_and_db_profiles(profiles, getattr(message, "guild", None))
                if profiles:
                    for term in candidate_terms:
                        m = find_matching_guild_member(term, profiles)
                        if m:
                            matched_member = m
                            matched_term = term
                            break
            except Exception as e:
                log.warning("user-first guild member lookup failed: %r", e)

        subject = matched_term or target_subj or cand_term or (candidate_terms[0] if candidate_terms else "")

        # 画像検索リクエスト時はメンバー名一致によるブロックをバイパス
        is_image_search = (
            "image_search_request" in rule_info.get("reasons", [])
            or "image_search_request" in rule_info.get("rule_reasons", [])
            or rule_info.get("mode") == "image_search"
        )

        if matched_member and not is_image_search:
            # サーバーメンバーが存在する場合：通常のWeb検索・外部調査をキャンセル
            rule_info["needs_research"] = False
            rule_info["should_consult_model"] = False
            rule_info["topic_opinion_flag"] = False
            rule_info["consultation_flag"] = False
            rule_info["unknown_term_flag"] = False
            disp_m = str(matched_member.get("current_display_name") or matched_member.get("current_name") or matched_term)
            runtime.matched_subject_member = matched_member
            member_note = f"ユーザー（{disp_m}）はこのDiscordサーバーの参加者・仲間である。"
            if member_note not in runtime.memory_context_lines:
                runtime.memory_context_lines.append(member_note)
            log.info("[USER_FIRST_RESOLUTION] BLOCKED: term='%s' is guild member", matched_term)
            log.info(
                "User-first priority: '%s' resolved to Discord guild member '%s' (uid=%s). Prioritizing user over web search.",
                matched_term,
                disp_m,
                matched_member.get("user_id"),
            )
        elif is_image_search:
            log.info("[USER_FIRST_RESOLUTION] BYPASS for image_search: term='%s' is guild member but request is image_search", matched_term)
        elif bool(rule_info.get("topic_opinion_flag")):
            # サーバーメンバーに該当しない場合のみ、記憶DB（ステップ1）やWeb調査（ステップ2）へ進む
            persona = cfg_persona_namespace(custom_cfg=cfg)
            aspect = str(rule_info.get("target_aspect") or "")
            found_memories = await check_memory_for_topic(
                memory_store,
                persona=persona,
                target_subject=subject,
                target_aspect=aspect,
                guild_id=gid,
            )
            if found_memories:
                rule_info["needs_research"] = False
                rule_info["should_consult_model"] = False
                from front.discord.common.bot_identity import resolve_bot_identity
                from harness.agent.tool_registry.web_research.topic_investigation import is_person_like_memory
                bot_ident = resolve_bot_identity(bot=self.bot, message=message)
                runtime.context_lines.append(f"（{bot_ident.primary_name}の記憶・実感: {' / '.join(found_memories)}）")
                runtime.recalled_topic_memories.extend(found_memories)
                for fm in found_memories:
                    if fm not in runtime.memory_context_lines:
                        runtime.memory_context_lines.append(fm)

                # 対象が記憶の中で人間・サーバーメンバーとして行動している場合、メンバーとしても解決
                if not matched_member and subject and is_person_like_memory(subject, found_memories):
                    matched_member = {
                        "user_id": 0,
                        "current_name": subject,
                        "current_display_name": subject,
                        "aliases": [],
                        "roles": [],
                        "from_memory": True,
                    }
                    runtime.matched_subject_member = matched_member
                    member_note = f"ユーザー（{subject}）はこのDiscordサーバーの参加者・仲間である。"
                    if member_note not in runtime.memory_context_lines:
                        runtime.memory_context_lines.append(member_note)
                    log.info("Topic memory entity resolution: '%s' resolved as guild member/friend from memories.", subject)

                log.info("Topic opinion inquiry: recalled memory for '%s': %r", subject, found_memories)
            else:
                log.info("Topic opinion inquiry: no memory for '%s', proceeding to web research", subject)

        base_decision = {
            "needs_research": bool(rule_info.get("needs_research")),
            "mode": self._normalize_research_mode(rule_info.get("mode"), fallback="simple_search"),
            "query": str(rule_info.get("query") or "").strip(),
            "reason": " / ".join(list(rule_info.get("rule_reasons") or [])[:2]),
            "provisional_reply": "" if bool(rule_info.get("topic_opinion_flag")) else self._default_research_provisional_text(rule_info),
            "recency_flag": bool(rule_info.get("recency_flag")),
            "unknown_term_flag": bool(rule_info.get("unknown_term_flag")),
            "source_request": bool(rule_info.get("source_request")),
            "url_present": bool(rule_info.get("url_present")),
            "x_timeline_flag": bool(rule_info.get("x_timeline_flag")),
            "x_search_flag": bool(rule_info.get("x_search_flag")),
            "conversational_followup": bool(rule_info.get("conversational_followup")),
            "rule_reasons": list(rule_info.get("rule_reasons") or []),
            "topic_opinion_flag": bool(rule_info.get("topic_opinion_flag")),
            "consultation_flag": bool(rule_info.get("consultation_flag")),
            "target_subject": str(rule_info.get("target_subject") or ""),
            "target_aspect": str(rule_info.get("target_aspect") or ""),
            "original_user_text": runtime.original_user_text,
        }

        model_result: dict[str, Any] = {}
        if bool(rule_info.get("should_consult_model")):
            model_fallback = cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_UTILITY_MODEL", cfg("OLLAMA_MODEL", "")))
            decision_model = str(cfg("REPLY_RESEARCH_DECISION_MODEL", model_fallback) or "")
            try:
                if decision_model:
                    runtime.utility_models_used.add(decision_model)
                model_result = await call_ollama_json(
                    build_reply_research_decider_prompt(
                        original_user_text=runtime.original_user_text,
                        effective_user_text=runtime.effective_user_text,
                        context_lines=runtime.context_lines,
                        rule_info=rule_info,
                    ),
                    system_prompt=build_reply_research_decider_system_prompt(),
                    schema=REPLY_RESEARCH_DECISION_SCHEMA,
                    think=False,
                    model=decision_model,
                    timeout_sec=self._research_decision_timeout_sec(),
                    retries=0,
                    temperature=0.1,
                    num_predict=max(cfg_int("REPLY_RESEARCH_DECISION_NUM_PREDICT", 192), 96),
                )
            except Exception as e:
                log.warning("reply research decision failed; using rules only: %r", e)

        user_text = str(runtime.original_user_text or "").strip()
        howto_keywords = (
            "方法", "やり方", "やりかた", "仕方", "しかた", "手順",
            "使い方", "つかいかた", "ダウンロード", "download", "保存",
            "ログイン", "サインイン", "login", "signin",
            "料金", "仕様", "仕組み", "しくみ",
        )
        is_howto_intent = any(kw in user_text.lower() for kw in howto_keywords)

        model_mode = self._normalize_research_mode(
            model_result.get("mode"),
            fallback="",
        )
        model_selected_web = model_mode in {"browser_search", "simple_search"}

        final_mode = self._normalize_research_mode(
            model_result.get("mode"),
            fallback=str(base_decision.get("mode") or "auto"),
        )
        if str(base_decision.get("mode") or "") == "img_thread":
            # img_thread はURL一般処理より優先する。汎用ブラウザ読みだと本文が崩れやすい。
            final_mode = "img_thread"
        elif str(base_decision.get("mode") or "") == "img_top5":
            # img_top5 はルール側で確定したモード。LLMの上書きを禁止する
            final_mode = "img_top5"
        elif base_decision["url_present"]:
            final_mode = "browser_read_url"
        elif model_selected_web:
            # モデルがWeb検索（browser_search等）を選択した場合は、ルールのx_search_flagによる上書きを防ぎモデルの推論を最優先
            final_mode = model_mode
        elif base_decision.get("topic_opinion_flag"):
            # 作品・トピックの評価・比較調査は上位記事の本文を読解して世間の評判を把握する
            final_mode = "browser_search"
        elif base_decision.get("consultation_flag"):
            # 困りごと・意見募集・解決策の模索はWeb解説・解決策検索を選択
            final_mode = model_mode if model_mode and model_mode != "x_search" else "browser_search"
        elif is_howto_intent:
            # How-to（やり方・操作手順・保存等）の質問は、SNSポスト検索ではなくWeb解説検索を選択
            final_mode = model_mode if model_mode and model_mode != "x_search" else "browser_search"
        elif base_decision.get("x_search_flag") or str(base_decision.get("mode") or "") == "x_search":
            final_mode = "x_search"
        elif base_decision["x_timeline_flag"]:
            final_mode = "x_timeline"

        final_query = str(model_result.get("search_query") or base_decision["query"] or "").strip()
        if final_query and not base_decision["url_present"] and final_mode not in {"img_top5", "img_thread", "x_timeline"}:
            # user_text に含まれていない Assistant/User/System ロール名や対話相手の名前が LLM により混入した場合は除去する (BAN-163, BAN-174)
            u_lower = user_text.lower()
            target_disp = str(getattr(runtime, "author_display_name", "") or getattr(runtime, "author_name", "") or "").strip()
            target_raw = str(getattr(runtime, "author_name", "") or "").strip()
            cleaned_tokens: list[str] = []
            for tok in final_query.split():
                tok_low = tok.lower()
                if tok_low in {"assistant", "user", "system", "bot"} and tok_low not in u_lower:
                    continue
                if target_disp and tok == target_disp and target_disp not in user_text:
                    continue
                if target_raw and tok == target_raw and target_raw not in user_text:
                    continue
                cleaned_tokens.append(tok)
            if cleaned_tokens:
                final_query = " ".join(cleaned_tokens)
        if base_decision["url_present"] or base_decision["x_timeline_flag"] or final_mode in {"img_top5", "img_thread"}:
            # URL/タイムライン/IMG は query を固定URLにする
            final_query = str(base_decision["query"] or final_query or "").strip()

        if final_mode == "img_thread" and final_query:
            # 複数URLが含まれている場合は先頭1件のみを抽出
            url_match = re.search(r"https?://(?:img|may)\.2chan\.net/b/res/\d+\.htm", final_query, re.IGNORECASE)
            if url_match:
                final_query = url_match.group(0)
            elif runtime.original_user_text:
                url_match_user = re.search(r"https?://(?:img|may)\.2chan\.net/b/res/\d+\.htm", runtime.original_user_text, re.IGNORECASE)
                if url_match_user:
                    final_query = url_match_user.group(0)

        final_decision = {
            **base_decision,
            "needs_research": bool(base_decision["needs_research"] or model_result.get("needs_research")),
            "mode": final_mode,
            "query": final_query,
            "reason": truncate_text(str(model_result.get("reason") or base_decision["reason"] or "").strip(), 40),
            "provisional_reply": self._choose_research_provisional_text(
                model_result=model_result,
                base_decision=base_decision,
                user_text=runtime.original_user_text,
            ),
            "recency_flag": bool(base_decision["recency_flag"] or model_result.get("recency_flag")),
            "unknown_term_flag": bool(base_decision["unknown_term_flag"] or model_result.get("unknown_term_flag")),
            "source_request": bool(base_decision["source_request"] or model_result.get("source_request")),
            "x_timeline_flag": bool(base_decision["x_timeline_flag"]),
            "x_search_flag": bool(base_decision.get("x_search_flag")),
        }
        if final_decision["provisional_reply"] == "SKIP":
            final_decision["provisional_reply"] = ""
        elif not final_decision["provisional_reply"] and final_decision["needs_research"]:
            final_decision["provisional_reply"] = self._default_research_provisional_text(final_decision)

        runtime.reply_research_decision = final_decision
        return final_decision

    async def _generate_research_backed_reply(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        *,
        base_prompt: str,
        last_assistant_text: str,
        intent_info: dict[str, Any],
        research_decision: dict[str, Any],
        research_result: dict[str, Any],
    ) -> str:
        if self._is_research_result_insufficient(research_result):
            # ユーザーが自然な会話・質問をしている場合（URL単体貼り付けや「URL確認して」等の明示的URL調査命令でない場合）、
            # 機械的な「404で取れなかった」定型文を即時返信せず、手元の会話コンテキストで通常推論を試みる
            user_text_val = str(runtime.original_user_text or "").strip()
            explicit_url_command = any(kw in user_text_val.lower() for kw in ("url", "リンク", "このサイト", "そのサイト", "記事見て", "ページ見て"))
            if not explicit_url_command and not bool(extract_urls(user_text_val)):
                log.info("research result insufficient but user text is conversational (%r); falling back to LLM context reply", user_text_val)
                try:
                    # 空の調査結果として扱い、通常のプロンプト生成でキャラクターとしての返答を生成
                    research_result["summary"] = ""
                    research_result["error"] = ""
                    # 以下の通常の prompt 構築・LLM呼び出しフローをそのまま続行
                except Exception as e:
                    log.warning("conversational fallback after research failure failed: %r", e)
                    return self._build_research_failure_reply(research_decision, research_result)
            else:
                return self._build_research_failure_reply(research_decision, research_result)

        research_mode = self._normalize_research_mode(
            research_result.get("mode") or research_decision.get("mode"),
            fallback="simple_search",
        )

        research_base_prompt = self._build_research_reply_base_prompt(
            runtime,
            message=message,
            last_assistant_text=last_assistant_text,
            intent_info=intent_info,
            topic_break="直前の流れとは別話題" in str(base_prompt or ""),
        )
        prompt = build_research_grounded_reply_prompt(
            base_prompt=research_base_prompt or base_prompt,
            research_summary=str(research_result.get("summary") or ""),
            research_query=str(research_result.get("query") or research_decision.get("query") or ""),
            research_mode=research_mode,
            rule_reason=str(research_decision.get("reason") or ""),
            recency_flag=bool(research_decision.get("recency_flag")),
            source_request=bool(research_decision.get("source_request")),
            unknown_term_flag=bool(research_decision.get("unknown_term_flag")),
            used_browser=bool(research_result.get("used_browser")),
            confidence=float(research_result.get("confidence") or 0.0),
            sources=list(research_result.get("sources") or []),
        )
        try:
            call_fn = getattr(self, "_call_reply_model_or_fallback", None)
            if callable(call_fn):
                reply = await call_fn(
                    message,
                    prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
            else:
                reply = await self._call_with_typing(
                    message,
                    prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
        except Exception:
            if research_mode == "img_thread":
                return self._build_img_thread_direct_reply(research_result)
            raise
        reply = await self._apply_retry_guards(
            message,
            prompt=prompt,
            reply=reply,
            last_assistant_text=last_assistant_text,
            intent_info=intent_info,
            runtime=runtime,
        )
        bot_user_id = runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        replacement_name = (
            runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or runtime.author_name
        ).strip()
        raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
        other_names_set = {raw_author_name} if raw_author_name and raw_author_name != replacement_name else None
        user_text_val = str(runtime.original_user_text or "").strip()
        cleaned = sanitize_generated_reply(
            reply,
            bot_user_id=bot_user_id,
            replacement_name=replacement_name,
            target_name=replacement_name,
            other_names=other_names_set,
            user_text=user_text_val,
        )

        is_topic_opinion = bool(research_decision.get("topic_opinion_flag")) or "topic_opinion_inquiry" in str(
            research_decision.get("reason") or ""
        )
        if is_topic_opinion and _looks_like_topic_opinion_evasion_reply(cleaned):
            log.warning("topic opinion inquiry evasion reply detected: %r; retrying with explicit guidance", cleaned[:60])
            retry_prompt = _build_topic_opinion_retry_prompt(prompt)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
                if retried:
                    retried_cleaned = sanitize_generated_reply(
                        retried,
                        bot_user_id=bot_user_id,
                        replacement_name=replacement_name,
                        user_text=user_text_val,
                    )
                    if retried_cleaned and not _looks_like_topic_opinion_evasion_reply(retried_cleaned):
                        cleaned = retried_cleaned
                    elif retried_cleaned:
                        cleaned = retried_cleaned
            except Exception as e:
                log.warning("topic opinion evasion retry failed: %r", e)

        research_summary_str = str(research_result.get("summary") or "").strip()
        is_consultation = bool(research_decision.get("consultation_flag")) or (
            "consultation_problem_solving" in str(research_decision.get("reason") or "")
        )
        if is_consultation and _looks_like_consultation_evasion_or_lecture(cleaned, research_summary_str, user_text_val):
            log.warning(
                "consultation evasion or long lecture reply detected: %r; retrying with concise solution guidance",
                cleaned[:60],
            )
            retry_prompt = _build_consultation_solution_retry_prompt(prompt, user_text=user_text_val)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
                if retried:
                    retried_cleaned = sanitize_generated_reply(
                        retried,
                        bot_user_id=bot_user_id,
                        replacement_name=replacement_name,
                        user_text=user_text_val,
                    )
                    if retried_cleaned and not _looks_like_consultation_evasion_or_lecture(
                        retried_cleaned, research_summary_str, user_text_val
                    ):
                        cleaned = retried_cleaned
                    elif retried_cleaned:
                        cleaned = retried_cleaned
            except Exception as e:
                log.warning("consultation solution retry failed: %r", e)
        if research_summary_str and _looks_like_raw_research_dump(cleaned, research_summary_str, user_text_val):
            log.warning(
                "raw research dump or one-sided lecture reply detected: %r; retrying with conversation guidance",
                cleaned[:60],
            )
            retry_prompt = _build_research_conversation_retry_prompt(prompt, user_text=user_text_val)
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
                if retried:
                    retried_cleaned = sanitize_generated_reply(
                        retried,
                        bot_user_id=bot_user_id,
                        replacement_name=replacement_name,
                        user_text=user_text_val,
                    )
                    if retried_cleaned and not _looks_like_raw_research_dump(
                        retried_cleaned, research_summary_str, user_text_val
                    ):
                        cleaned = retried_cleaned
                    elif retried_cleaned:
                        cleaned = retried_cleaned
            except Exception as e:
                log.warning("raw research dump retry failed: %r", e)

        meta_failure_detected = _looks_like_meta_search_failure_report(cleaned, user_text_val)
        if meta_failure_detected:
            log.warning(
                "meta search failure report detected for non-question reaction: %r; retrying with contextual reaction guidance",
                cleaned[:60],
            )
            retry_prompt = _build_contextual_reaction_retry_prompt(
                prompt,
                user_text=user_text_val,
                context_lines=runtime.context_lines,
                intent_info=intent_info,
            )
            try:
                retried = await self._call_with_typing(
                    message,
                    retry_prompt,
                    system_prompt=self._reply_system_prompt(),
                    images=runtime.media.images,
                    num_predict=self._research_reply_num_predict(),
                )
                if retried:
                    retried_cleaned = sanitize_generated_reply(
                        retried,
                        bot_user_id=bot_user_id,
                        replacement_name=replacement_name,
                        target_name=replacement_name,
                        other_names=other_names_set,
                        user_text=user_text_val,
                    )
                    if retried_cleaned and not _looks_like_meta_search_failure_report(
                        retried_cleaned, user_text_val
                    ):
                        cleaned = retried_cleaned
                    elif retried_cleaned:
                        cleaned = retried_cleaned
            except Exception as e:
                log.warning("contextual reaction retry failed: %r", e)

        if (
            research_mode == "img_thread"
            and (not cleaned or self._is_configured_failure_reply(cleaned) or looks_like_abnormal_assistant_reply(cleaned))
        ):
            return self._build_img_thread_direct_reply(research_result)
        if cleaned:
            if meta_failure_detected:
                return cleaned
            return self._append_research_citations(cleaned, research_result)
        return self._build_research_failure_reply(research_decision, research_result)

    def _store_channel_research_cache(self, cid: int | None, result: dict[str, Any]) -> None:
        if cid is None or not result:
            return
        mode = str(result.get("mode") or "").strip()
        if mode not in {"img_top5", "x_timeline"}:
            return

        if not isinstance(getattr(self, "_channel_research_cache", None), dict):
            self._channel_research_cache = {}
            
        import time as _time
        self._channel_research_cache[cid] = {
            "mode": mode,
            "summary": str(result.get("summary") or ""),
            "sources": list(result.get("sources") or []),
            "threads": list(result.get("threads") or []),
            "query": str(result.get("query") or ""),
            "timestamp": _time.monotonic(),
            "channel_id": cid,
        }

    async def _resolve_research_with_deepdive(
        self,
        message: discord.Message,
        runtime: MessageRuntime,
        research_decision: dict[str, Any],
        research_context: str,
    ) -> tuple[dict[str, Any], bool]:
        cid = channel_id(message)
        cache = self._channel_research_cache.get(cid) if cid is not None else None
        
        deepdive_info = detect_deepdive_followup(runtime.original_user_text, cache)
        
        if deepdive_info.get("is_deepdive"):
            deepdive_mode = deepdive_info.get("deepdive_mode")
            if deepdive_mode == "img_thread":
                target_url = deepdive_info.get("target_url")
                if target_url:
                    first_url = _extract_first_hutaba_thread_url(str(target_url), helper=self._get_img2chan_helper())
                    if first_url:
                        target_url = first_url
                if not target_url:
                    target_url = "https://img.2chan.net/b/futaba.php?mode=cat&sort=6"
                try:
                    result = await research_dispatch(target_url, context=research_context, mode="img_thread")
                    return result, True
                except Exception as e:
                    return {"error": str(e)}, True
            elif deepdive_mode == "x_deepdive" and cache:
                fake_result = {
                    "mode": "x_timeline",
                    "summary": cache.get("summary", ""),
                    "sources": cache.get("sources", []),
                    "used_web": True,
                    "used_browser": False,
                }
                return fake_result, True
        
        # 通常の調査フロー
        norm_mode = self._normalize_research_mode(research_decision.get("mode"))
        if norm_mode == "img_thread":
            query_url = str(research_decision.get("query") or "").strip()
            first_url = _extract_first_hutaba_thread_url(
                f"{query_url} {runtime.original_user_text}",
                helper=self._get_img2chan_helper(),
            )
            research_input = first_url or query_url or runtime.original_user_text
        elif norm_mode in {"browser_read_url", "x_timeline"}:
            research_input = runtime.original_user_text
        elif norm_mode != "img_top5":
            research_input = str(research_decision.get("query") or runtime.effective_user_text or runtime.original_user_text)
        else:
            research_input = str(research_decision.get("query") or "https://img.2chan.net/b/futaba.php?mode=cat&sort=6")
        if self._normalize_research_mode(research_decision.get("mode")) == "image_search":
            from front.discord.common.message_media import extract_first_image_url, resolve_message_with_media
            target_msg = await resolve_message_with_media(message)
            img_url = extract_first_image_url(target_msg)
            if img_url:
                research_input = img_url
            log.info(
                "[IMAGE_SEARCH_EVENT] Resolved image research for message_id=%s: target_msg_id=%s, img_url='%s'",
                getattr(message, "id", None), getattr(target_msg, "id", None), img_url or ""
            )
        try:
            result = await research_dispatch(
                research_input,
                context=research_context,
                mode=self._normalize_research_mode(research_decision.get("mode"), fallback="simple_search"),  # type: ignore[arg-type]
                max_results=4,
            )
            return result, False
        except Exception as e:
            log.warning("reply research dispatch failed: %r", e)
            return {
                "used_web": False,
                "used_browser": False,
                "summary": "",
                "sources": [],
                "confidence": 0.0,
                "mode": self._normalize_research_mode(research_decision.get("mode"), fallback="simple_search"),
                "query": str(research_decision.get("query") or research_input or "").strip(),
                "error": f"調査中にエラーが発生した: {e}",
            }, False

    async def _send_timeout_fallback_reply(
        self,
        message: discord.Message,
        resolved_runtime: MessageRuntime,
        *,
        bot_user_id: int | None = None,
        provisional_message: discord.Message | None = None,
        is_direct: bool = False,
    ) -> discord.Message | None:
        fallback_text = _reply_model_timeout_fallback()
        _eff_target = (
            resolved_runtime.author_display_name
            or (display_name(message) if message is not None else "")
            or resolved_runtime.author_name
            or "ユーザー"
        ).strip()
        _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
        _eff_other_names = {
            spk
            for line in (resolved_runtime.context_lines or [])
            if (spk := _extract_speaker_from_context_line(line)) and spk != _eff_target
        }
        if _raw_author_name and _raw_author_name != _eff_target:
            _eff_other_names.add(_raw_author_name)
        reply = sanitize_generated_reply(
            fallback_text,
            bot_user_id=bot_user_id,
            replacement_name=_eff_target,
            target_name=_eff_target,
            other_names=_eff_other_names,
        )
        sent: discord.Message | None = None
        if provisional_message is not None:
            try:
                sent = await self._send_followup_reply(provisional_message, reply)
            except Exception as e:
                log.warning("followup timeout fallback reply failed; falling back to single final reply: %r", e)
                with contextlib.suppress(Exception):
                    await provisional_message.delete()
                sent = await self._send_reply(message, reply, is_direct=is_direct)
        else:
            sent = await self._send_reply(message, reply, is_direct=is_direct)
        if sent is not None:
            self._append_sent_message(sent)
        return sent

    async def _send_contextual_reaction(
        self,
        message: discord.Message,
        runtime: MessageRuntime | None = None,
        incoming_emotion_scores: dict[str, float] | None = None,
        *,
        force_response: bool = False,
    ) -> None:
        resolved_runtime = runtime or await self._get_or_build_runtime(message)
        if resolved_runtime is None:
            return

        cid = channel_id(message)
        primary_cid = cfg_primary_channel_id(0)
        is_primary = primary_cid > 0 and (cid == primary_cid or _get_channel_kind(message) == "primary")

        bot_user_id = resolved_runtime.bot_user_id or (getattr(getattr(self, "bot", None), "user", None) and getattr(self.bot.user, "id", None))
        is_direct = bool(force_response)
        if not is_direct and bot_user_id is not None:
            with contextlib.suppress(Exception):
                is_direct = await _is_force_response_trigger(message, bot_user_id)

        umigame_running = cid is not None and cid in self.umigame_states

        if getattr(resolved_runtime, "is_twenty_doors_reply", False):
            handled = await self._handle_active_20doors_message(message, resolved_runtime)
            if handled:
                return

        if umigame_running:
            if not getattr(resolved_runtime, "is_umigame_reply", False):
                return
            handled = await self._handle_active_umigame_message(message, resolved_runtime)
            if handled:
                return
        else:
            if await self._handle_time(message, resolved_runtime, is_direct=is_direct):
                return
            if await self._handle_weather(message, resolved_runtime, is_direct=is_direct):
                return

        pre_last_assistant_text = await _find_last_assistant_message_text(
            message,
            bot_user_id=resolved_runtime.bot_user_id,
            cache=self.channel_context_cache,
        )
        research_decision = dict(getattr(resolved_runtime, "reply_research_decision", {}) or {})
        needs_research = self._should_use_research_reply(research_decision)
        flow = await _check_context_flow(
            message,
            resolved_runtime.context_lines,
            latest_user_text=resolved_runtime.effective_user_text,
            last_assistant_text=pre_last_assistant_text,
        )

        # このターンで実際に触った utility tier だけを解放し、main tier のVRAMを空ける。
        # ただし直後の事前分析（Pre-reply Analyzer）で使用される OLLAMA_MIDDLE_MODEL は
        # 早期アンロードによる直後の再ロード待機（約12秒）を防止するため、ここでの解放対象から除外する。
        middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
        should_release_configured = cfg_bool("OLLAMA_RELEASE_CONFIGURED_UTILITY_MODELS_BEFORE_REPLY", False)
        _models_to_release = {
            str(m).strip()
            for m in getattr(resolved_runtime, "utility_models_used", set())
            if str(m).strip() and (not middle_model or str(m).strip() != middle_model)
        }
        if should_release_configured:
            _classifier_model = str(cfg("OLLAMA_CLASSIFIER_MODEL", "") or "").strip()
            if _classifier_model and _classifier_model != middle_model:
                _models_to_release.add(_classifier_model)
        for _m in _models_to_release:
            await release_ollama_model(
                _m,
                timeout_sec=max(cfg_float("OLLAMA_UTILITY_RELEASE_TIMEOUT_SEC", 2.0), 0.2),
            )
        del _models_to_release

        middle_model_released = False

        async def _release_middle_model_if_needed() -> None:
            nonlocal middle_model_released
            if middle_model_released:
                return
            middle_model_released = True
            if middle_model and (
                should_release_configured
                or middle_model in getattr(resolved_runtime, "utility_models_used", set())
            ):
                await release_ollama_model(
                    middle_model,
                    timeout_sec=max(cfg_float("OLLAMA_UTILITY_RELEASE_TIMEOUT_SEC", 2.0), 0.2),
                )

        try:
            try:
                if flow == "BREAK":
                    log.info("Topic break detected: sending off-topic/unclear message to LLM.")
                    if needs_research:
                        prompt, last_assistant_text, intent_info = await self._build_research_break_base_prompt(
                            message,
                            resolved_runtime,
                            incoming_emotion_scores=incoming_emotion_scores,
                        )
                        last_assistant_text = pre_last_assistant_text or last_assistant_text
                        reply = ""
                    else:
                        prompt, reply, last_assistant_text, intent_info = await self._generate_break_reply(
                            message,
                            resolved_runtime,
                            incoming_emotion_scores=incoming_emotion_scores,
                            skip_model=False,
                            allow_unclear_intent_fallback=True,
                        )
                    if pre_last_assistant_text and not last_assistant_text:
                        last_assistant_text = pre_last_assistant_text
                    handled_message_ids: list[int] = []
                    context_lines = resolved_runtime.context_lines
                    spontaneous_source = str(intent_info.get("spontaneous_source") or "").strip()
                else:
                    prompt, reply, last_assistant_text, intent_info, handled_message_ids, context_lines = await self._generate_normal_reply(
                        message,
                        resolved_runtime,
                        incoming_emotion_scores=incoming_emotion_scores,
                        skip_model=needs_research,
                        allow_unclear_intent_fallback=not needs_research,
                    )
                    spontaneous_source = ""
            except ReplyModelTimeoutError as e:
                if not should_send_timeout_fallback(
                    is_primary_channel=is_primary,
                    is_direct=is_direct,
                ):
                    log.warning(
                        "Reply skipped because model call timed out after retries channel=%s message=%s: %r",
                        cid,
                        getattr(message, "id", None),
                        e,
                    )
                    return
                log.warning(
                    "Model call timed out after retries; sending timeout fallback reply channel=%s message=%s is_primary=%s is_direct=%s: %r",
                    cid,
                    getattr(message, "id", None),
                    is_primary,
                    is_direct,
                    e,
                )
                self._append_current_user_message(resolved_runtime, message)
                await _release_middle_model_if_needed()
                await self._send_timeout_fallback_reply(
                    message,
                    resolved_runtime,
                    bot_user_id=bot_user_id,
                    is_direct=is_direct,
                )
                return

            self._append_current_user_message(resolved_runtime, message)

            # Pre-reply Analyzer LLM の判定を尊重した調停（BAN-115）
            from front.discord.reply.reply_research_planner import reconcile_research_decision
            research_decision, needs_research = reconcile_research_decision(
                research_decision, intent_info
            )
            resolved_runtime.reply_research_decision = research_decision

            if not needs_research and not reply:
                log.info("[RESEARCH_PLANNER] Falling back to standard reply generation after research suppression")
                user_raw_text = resolved_runtime.effective_user_text or resolved_runtime.original_user_text or content(message)
                should_think_fn = getattr(self, "_should_enable_adaptive_thinking", None)
                adaptive_think = should_think_fn(intent_info, user_text=user_raw_text) if callable(should_think_fn) else False
                think_override = True if adaptive_think else None
                is_think = bool(adaptive_think or cfg_bool("OLLAMA_MAIN_MODEL_THINK", False))

                reply_system = self._reply_system_prompt()
                clean_fn = getattr(self, "_clean_duplicate_prompt_instructions", None)
                cleaned_prompt = clean_fn(prompt, reply_system) if callable(clean_fn) else prompt
                num_predict_fn = getattr(self, "_reply_num_predict", None)
                num_predict_val = (
                    num_predict_fn(
                        intent_info=intent_info,
                        prompt_text=cleaned_prompt,
                        system_prompt=reply_system,
                        think=is_think,
                    )
                    if callable(num_predict_fn)
                    else 256
                )
                call_fn = getattr(self, "_call_reply_model_or_fallback", None)
                if callable(call_fn):
                    reply = await call_fn(
                        message,
                        cleaned_prompt,
                        system_prompt=reply_system,
                        images=resolved_runtime.media.images,
                        num_predict=num_predict_val,
                        think_override=think_override,
                    )
                else:
                    reply = await self._call_with_typing(
                        message,
                        cleaned_prompt,
                        images=resolved_runtime.media.images,
                        system_prompt=reply_system,
                        num_predict=num_predict_val,
                    )

            if needs_research:
                provisional_message: discord.Message | None = None
                provisional_text = (
                    str(research_decision.get("provisional_reply", "")).strip()
                    if cfg_bool("REPLY_RESEARCH_PROVISIONAL_ENABLED", False)
                    else ""
                )
                if provisional_text:
                    try:
                        provisional_message = await self._send_provisional_reply(message, provisional_text, is_direct=is_direct)
                    except Exception as e:
                        log.warning("provisional reply failed; falling back to single final reply: %r", e)

                research_context = self._build_research_context_text(resolved_runtime)
                research_result, _ = await self._resolve_research_with_deepdive(
                    message, resolved_runtime, research_decision, research_context
                )
                if cid is not None:
                    self._store_channel_research_cache(cid, research_result)

                try:
                    reply = await self._generate_research_backed_reply(
                        message,
                        resolved_runtime,
                        base_prompt=prompt,
                        last_assistant_text=last_assistant_text,
                        intent_info=intent_info,
                        research_decision=research_decision,
                        research_result=research_result,
                    )
                except ReplyModelTimeoutError as e:
                    if not should_send_timeout_fallback(
                        is_primary_channel=is_primary,
                        is_direct=is_direct,
                    ):
                        log.warning(
                            "Research reply skipped because model call timed out after retries channel=%s message=%s: %r",
                            cid,
                            getattr(message, "id", None),
                            e,
                        )
                        if provisional_message is not None:
                            with contextlib.suppress(Exception):
                                await provisional_message.delete()
                        return
                    log.warning(
                        "Research model call timed out after retries; sending timeout fallback reply channel=%s message=%s is_primary=%s is_direct=%s: %r",
                        cid,
                        getattr(message, "id", None),
                        is_primary,
                        is_direct,
                        e,
                    )
                    await self._send_timeout_fallback_reply(
                        message,
                        resolved_runtime,
                        bot_user_id=bot_user_id,
                        provisional_message=provisional_message,
                        is_direct=is_direct,
                    )
                    return
                _eff_target = (
                    resolved_runtime.author_display_name
                    or (display_name(message) if message is not None else "")
                    or resolved_runtime.author_name
                    or "ユーザー"
                ).strip()
                _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
                _eff_other_names = {
                    spk
                    for line in (resolved_runtime.context_lines or [])
                    if (spk := _extract_speaker_from_context_line(line)) and spk != _eff_target
                }
                if _raw_author_name and _raw_author_name != _eff_target:
                    _eff_other_names.add(_raw_author_name)
                reply = sanitize_generated_reply(
                    reply,
                    bot_user_id=bot_user_id,
                    replacement_name=_eff_target,
                    target_name=_eff_target,
                    other_names=_eff_other_names,
                )

                if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                    puzzle = self.umigame_states.pop(cid, None)
                    if puzzle:
                        reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

                if should_skip_unknown_reply(
                    reply,
                    user_text=resolved_runtime.original_user_text,
                    is_primary_channel=is_primary,
                    is_direct=is_direct,
                ):
                    log.info(
                        "Research reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                        reply,
                        getattr(message, "id", None),
                    )
                    if provisional_message is not None:
                        with contextlib.suppress(Exception):
                            await provisional_message.delete()
                    return
                elif looks_like_unknown_or_incomprehension_reply(reply, user_text=resolved_runtime.original_user_text):
                    if resolved_runtime.matched_subject_member or resolved_runtime.recalled_topic_memories:
                        cleaned_r = sanitize_unknown_denial_for_known_subject(reply)
                        if cleaned_r != reply:
                            reply = cleaned_r
                            log.info("Sanitized unknown denial in research reply for known topic/member: %r", reply)
                    log.info(
                        "Research reply retained despite unknown expression due to primary/direct exception (is_primary=%s, is_direct=%s): %r (message_id=%s)",
                        is_primary,
                        is_direct,
                        reply,
                        getattr(message, "id", None),
                    )

                if provisional_message is not None:
                    try:
                        sent = await self._send_followup_reply(provisional_message, reply)
                    except Exception as e:
                        log.warning("followup reply failed; falling back to single final reply: %r", e)
                        sent = await self._send_reply(message, reply, is_direct=is_direct)
                else:
                    sent = await self._send_reply(message, reply, is_direct=is_direct)
                if sent is None:
                    log.info("research reply was dropped as stale channel=%s message_id=%s", cid, getattr(message, "id", None))
                    if provisional_message is not None:
                        with contextlib.suppress(Exception):
                            await provisional_message.delete()
                    return
                self._append_sent_message(sent)
                resolved_runtime.assistant_text = reply

                async def _bg_summary_task_research() -> None:
                    delay_sec = max(cfg_float("MEMORY_EXTRACTION_INITIAL_DELAY_SEC", 3.0), 0.1)
                    await asyncio.sleep(delay_sec)
                    bg_lock = getattr(self, "_bg_llm_lock", None)
                    if bg_lock is not None:
                        async with bg_lock:
                            await self._store_and_update_summary(
                                message,
                                sent,
                                runtime=resolved_runtime,
                                context_lines=context_lines,
                                assistant_text=reply,
                                incoming_emotion_scores=incoming_emotion_scores,
                                spontaneous_source=spontaneous_source,
                                research_result=research_result,
                                episode_context=intent_info,
                            )
                    else:
                        await self._store_and_update_summary(
                            message,
                            sent,
                            runtime=resolved_runtime,
                            context_lines=context_lines,
                            assistant_text=reply,
                            incoming_emotion_scores=incoming_emotion_scores,
                            spontaneous_source=spontaneous_source,
                            research_result=research_result,
                            episode_context=intent_info,
                        )

                _bg_task = asyncio.create_task(_bg_summary_task_research())
                def _on_summary_done_research(t: asyncio.Task[Any]) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        log.warning("_store_and_update_summary failed in background: %r", exc)

                _bg_task.add_done_callback(_on_summary_done_research)
            else:
                reply = await self._apply_retry_guards(
                    message,
                    prompt=prompt,
                    reply=reply,
                    last_assistant_text=last_assistant_text,
                    intent_info=intent_info,
                    runtime=resolved_runtime,
                )
                _eff_target = (
                    resolved_runtime.author_display_name
                    or (display_name(message) if message is not None else "")
                    or resolved_runtime.author_name
                    or "ユーザー"
                ).strip()
                _raw_author_name = getattr(getattr(message, "author", None), "name", "").strip()
                _eff_other_names = {
                    spk
                    for line in (resolved_runtime.context_lines or [])
                    if (spk := _extract_speaker_from_context_line(line)) and spk != _eff_target
                }
                if _raw_author_name and _raw_author_name != _eff_target:
                    _eff_other_names.add(_raw_author_name)
                reply = sanitize_generated_reply(
                    reply,
                    bot_user_id=bot_user_id,
                    replacement_name=_eff_target,
                    target_name=_eff_target,
                    other_names=_eff_other_names,
                )

                if umigame_running and _looks_like_umigame_clear(reply) and cid is not None:
                    puzzle = self.umigame_states.pop(cid, None)
                    if puzzle:
                        reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

                if should_skip_unknown_reply(
                    reply,
                    user_text=resolved_runtime.original_user_text,
                    is_primary_channel=is_primary,
                    is_direct=is_direct,
                ):
                    log.info(
                        "Reply skipped because assistant expressed unknown/incomprehension: %r (message_id=%s)",
                        reply,
                        getattr(message, "id", None),
                    )
                    return
                elif looks_like_unknown_or_incomprehension_reply(reply, user_text=resolved_runtime.original_user_text):
                    if resolved_runtime.matched_subject_member or resolved_runtime.recalled_topic_memories:
                        cleaned_r = sanitize_unknown_denial_for_known_subject(reply)
                        if cleaned_r != reply:
                            reply = cleaned_r
                            log.info("Sanitized unknown denial in reply for known topic/member: %r", reply)
                    log.info(
                        "Reply retained despite unknown expression due to primary/direct exception (is_primary=%s, is_direct=%s): %r (message_id=%s)",
                        is_primary,
                        is_direct,
                        reply,
                        getattr(message, "id", None),
                    )

                sent = await self._send_reply(message, reply, is_direct=is_direct)
                if sent is None:
                    log.info("reply was dropped as stale channel=%s message_id=%s", cid, getattr(message, "id", None))
                    return
                self._append_sent_message(sent)
                resolved_runtime.assistant_text = reply
                salvaged_res: dict[str, Any] | None = getattr(resolved_runtime, "salvaged_research_result", None)
                if not salvaged_res and isinstance(intent_info, dict):
                    salvaged_res = intent_info.get("salvaged_research_result")

                async def _bg_summary_task_regular() -> None:
                    delay_sec = max(cfg_float("MEMORY_EXTRACTION_INITIAL_DELAY_SEC", 3.0), 0.1)
                    await asyncio.sleep(delay_sec)
                    bg_lock = getattr(self, "_bg_llm_lock", None)
                    if bg_lock is not None:
                        async with bg_lock:
                            await self._store_and_update_summary(
                                message,
                                sent,
                                runtime=resolved_runtime,
                                context_lines=context_lines,
                                assistant_text=reply,
                                incoming_emotion_scores=incoming_emotion_scores,
                                spontaneous_source=spontaneous_source,
                                research_result=salvaged_res,
                                episode_context=intent_info,
                            )
                    else:
                        await self._store_and_update_summary(
                            message,
                            sent,
                            runtime=resolved_runtime,
                            context_lines=context_lines,
                            assistant_text=reply,
                            incoming_emotion_scores=incoming_emotion_scores,
                            spontaneous_source=spontaneous_source,
                            research_result=salvaged_res,
                            episode_context=intent_info,
                        )

                _bg_task = asyncio.create_task(_bg_summary_task_regular())
                def _on_summary_done_regular(t: asyncio.Task[Any]) -> None:
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        log.warning("_store_and_update_summary failed in background: %r", exc)

                _bg_task.add_done_callback(_on_summary_done_regular)
        finally:
            await _release_middle_model_if_needed()




