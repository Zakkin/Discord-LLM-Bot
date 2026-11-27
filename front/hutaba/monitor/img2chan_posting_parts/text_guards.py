"""スレ種別フィルタ、投稿後感情理由、盗用/キメラ/文数/文頭エコー/本文サニタイズ等の純粋関数群。"""
from __future__ import annotations

import contextlib
import difflib
import re
import unicodedata
from typing import Any, Iterable, Sequence

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _collect_self_names,
    _get_img2chan_helper,
    _is_self_name_guard_enabled,
    _normalize_img_post_no,
    _strip_self_name_vocative,
    cfg,
    cfg_bool,
    cfg_float,
    extract_first_user_facing_reply,
    log,
    sanitize_generated_reply,
    truncate_text,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _HUTABA_TOPIC_STOPWORDS,
    _extract_hutaba_non_quote_body,
    _strip_hutaba_quote_lines,
)


def _post_shape_observer(context: Any) -> None:
    """投稿の形（行数・文字数）が生成契約を大きく超えていないかを観測する（遮断しない）。"""
    body = _extract_hutaba_non_quote_body(context.text)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) > 3 or len(body) > 220:
        log.warning(
            "[hutaba-hooks] post_shape_observer: lines=%d chars=%d (contract: <=3 lines, <=220 chars) url=%s",
            len(lines), len(body), context.metadata.get("thread_url", ""),
        )
    return None


_SELF_PROJECTION_SYMPATHY_RE = re.compile(
    r"(?:私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち)も[^\n。！？!?]{0,35}?"
    r"(?:困ったこと(?:が)?ある|探してて困った|悩んだこと(?:が)?ある|途方に暮れた|"
    r"分からなくて困った|わからなくて困った|経験(?:が)?あるんですよ|同じこと思った)",
    re.IGNORECASE,
)
# ※ "RSS" 単体は質問文トピック名詞であり「RSSはやめてる所多いですよね」等の相槌行に現れるため、解決策アクション語から除外
_SOLUTION_HINT_MARKER_RE = re.compile(
    r"(?:クロール|スクレイピング|チェック|確認|探すと|探ったら|残って|放置|ツール|スクリプト|拡張機能|通知|差分|巡回|代替|使うと|すれば|しか無い|しかない)",
    re.IGNORECASE,
)
_HUTABA_META_SEARCH_REPORT_RE = re.compile(
    r"(?:調べてみた(?:けど|んですが|んですけど|ところ)|検索してみた(?:けど|んですが|んですけど)|"
    r"ググってみた(?:けど|んですが|んですけど)|"
    r"定義までは確認でき|意味までは確認でき|詳細までは確認でき|"
    r"ニュースばかりが並|記事ばかりが並|パーティーとか[^\n。！？!?]{0,20}ばかり|"
    r"特定の作品のネタか|別の固有名詞のことですか|何のことか確認でき)",
    re.IGNORECASE,
)
_HUTABA_META_COMMENTARY_RE = re.compile(
    r"(?:(?:ツッコミ|指摘|声|レス|意見|判定)(?:が|も)?(?:来て|入って|あって|言われて|飛んで)?(?:ます|る|ますけど|るけど|るようですが|そうですけど)|"
    r"と言われて(?:ます|る|います)|とツッコミ(?:が|入って)|って言われてる)",
    re.IGNORECASE,
)


_HUTABA_HONORIFIC_NAME_PATTERN = re.compile(
    r"(?:(?<=[^\w])|(?<=^))([A-Za-z0-9_\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]{1,16})(?:さん|氏|くん|ちゃん|様)(?=[、。！？\s　]|$)",
    flags=re.UNICODE,
)


def _clean_hutaba_thought_tags(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


_STREAM_URL_PATTERN = re.compile(
    r"(?:"
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?|live/|shorts/)|youtu\.be/)|"
    r"https?://(?:www\.)?twitch\.tv/|"
    r"https?://(?:live\d*\.)?nicovideo\.jp/|"
    r"https?://(?:www\.)?twitcasting\.tv/|"
    r"https?://(?:www\.)?showroom-live\.com/|"
    r"https?://(?:www\.)?openrec\.tv/|"
    r"(?:bin/)?jump\.php\?.*?(?:youtube\.com|youtu\.be|twitch\.tv|nicovideo\.jp)"
    r")",
    flags=re.IGNORECASE,
)

_STREAM_TITLE_KEYWORD_PATTERN = re.compile(
    r"(?:"
    r"【[^】]*(?:配信|実況|歌枠|生放送|耐久|同時視聴|お披露目|記念|リレー|LIVE|Live)[^】]*】|"
    r"(?:生?配信|生放送|実況|歌枠|耐久配信|飲酒配信|同時視聴|作業配信|朝活|メン限|"
    r"歌枠リレー|建てるぜ|次スレ|避難所|本スレ)"
    r")",
    flags=re.IGNORECASE,
)


def _is_img2chan_stream_or_live_thread(thread_item: dict[str, Any]) -> str | None:
    """配信・実況スレであるかを判定し、該当する場合は理由文字列を返す（健全な雑談スレなら None）。"""
    if not cfg_bool("HUTABA_EXCLUDE_STREAM_THREADS", True):
        return None

    text = str((thread_item or {}).get("text") or "")
    title = str((thread_item or {}).get("title") or "")
    full_content = f"{title}\n{text}".strip()

    # 1. 配信・動画URLの検知
    if _STREAM_URL_PATTERN.search(full_content):
        return "stream_url_detected"

    # 2. 配信・実況特有キーワードの検知
    if _STREAM_TITLE_KEYWORD_PATTERN.search(title) or _STREAM_TITLE_KEYWORD_PATTERN.search(text[:80]):
        return "stream_keyword_detected"

    return None


_REQUEST_THREAD_PATTERN = re.compile(
    r"(?:"
    # 1. 括弧内のリクエスト・お題表記
    r"【[^】]*(?:リクエスト|リク|お題募集|ネタ募集)[^】]*】|"
    # 2. 接頭辞（AI, 画像生成, イラスト, お絵描き, 手描き, 絵描き, 絵, コラ） + リクエスト/リク
    r"(?:AI|画像生成|イラスト|お絵描き|手描き|絵描き|絵|コラ)\s*(?:リクエスト|リク)(?:スレ|受付|募集|会場|所|部屋)?|"
    # 3. リクエスト/リク + 接尾辞（スレ, 受付, 募集, 会場, 所, 部屋）
    r"(?:リクエスト|リク)\s*(?:スレ|受付|募集|会場|所|部屋)|"
    # 4. リクエスト/リク + 呼びかけ・促し（どうぞ, ある, あれば, あります, ください, 待ってます, お願いします, はこちら）
    r"(?:リクエスト|リク)\s*(?:どうぞ|ある|あれば|あります|ください|待ってます|お願いします|はこちら)|"
    # 5. お題・ネタの募集
    r"(?:お題|ネタ)\s*(?:募集|リクエスト)(?:スレ)?"
    r")",
    flags=re.IGNORECASE,
)


def _is_img2chan_request_thread(thread_item: dict[str, Any]) -> str | None:
    """リクエスト・お題募集スレであるかを判定し、該当する場合は理由文字列を返す（健全な雑談スレなら None）。"""
    if not cfg_bool("HUTABA_EXCLUDE_REQUEST_THREADS", True):
        return None

    title = str((thread_item or {}).get("title") or "")
    text = str((thread_item or {}).get("text") or (thread_item or {}).get("op_text") or "")

    # 1. タイトルチェック
    if _REQUEST_THREAD_PATTERN.search(title):
        return "request_title_detected"

    # 2. 本文冒頭（100文字）チェック
    if _REQUEST_THREAD_PATTERN.search(text[:100]):
        return "request_keyword_detected"

    return None


def _is_hutaba_thread_expiring_soon(
    thread_or_item: dict[str, Any] | None,
    *,
    min_remaining_sec: float | None = None,
) -> str | None:
    """スレッドが消滅間近（デフォルト10分=600秒以内、または古いスレ警告あり）か判定する。
    除外すべき場合は理由文字列を返し、問題なければ None を返す。
    """
    if not isinstance(thread_or_item, dict):
        return None
    if not cfg_bool("HUTABA_EXCLUDE_EXPIRING_THREADS", True):
        return None

    threshold_sec = (
        float(min_remaining_sec)
        if min_remaining_sec is not None
        else max(60.0, cfg_float("HUTABA_MIN_THREAD_REMAINING_SEC", 600.0))
    )

    if bool(thread_or_item.get("has_old_warning")):
        return "old_thread_warning"

    rem_sec = thread_or_item.get("remaining_sec")
    if isinstance(rem_sec, (int, float)) and not isinstance(rem_sec, bool):
        if float(rem_sec) <= threshold_sec:
            return f"remaining_sec_too_short:{int(float(rem_sec))}s<={int(threshold_sec)}s"
        return None

    if bool(thread_or_item.get("is_expiring_soon")):
        return "expiring_soon_flag"

    # 生テキスト（expire_text 等）のみが存在する場合のフォールバック解析
    raw_expire = str(thread_or_item.get("expire_text") or "").strip()
    if raw_expire:
        helper = _get_img2chan_helper()
        parse_exp = getattr(helper, "parse_hutaba_thread_expiration", None)
        if callable(parse_exp):
            exp_info = parse_exp(raw_expire, min_remaining_sec=threshold_sec)
            if isinstance(exp_info, dict) and bool(exp_info.get("is_expiring_soon")):
                calc_rem = exp_info.get("remaining_sec")
                if isinstance(calc_rem, (int, float)):
                    return f"remaining_sec_too_short:{int(float(calc_rem))}s<={int(threshold_sec)}s"
                return "expiring_soon_parsed"

    return None


def _extract_hutaba_thread_topic_snippet(thread: dict[str, Any] | None, max_len: int = 18) -> str:
    """スレッドタイトルまたはOP本文から簡潔な話題スニペットを抽出。"""
    t = thread if isinstance(thread, dict) else {}
    title = str(t.get("title") or "").strip()
    if not title:
        title = str(t.get("op_text") or "").strip()
    # URLや余計な空白・改行・記号を除去
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[\r\n\t]+", " ", title).strip()
    title = title.strip(" 　!！?？#＃@＠")
    if not title:
        return "雑談"
    if len(title) > max_len:
        return title[:max_len] + "…"
    return title


def _extract_hutaba_comment_snippet(comment: str, max_len: int = 14) -> str:
    """投稿コメントから引用行（>）や改行を除去した簡潔な要約を抽出。"""
    cleaned = re.sub(r"^>.*$", "", comment, flags=re.MULTILINE)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).strip()
    if not cleaned:
        cleaned = comment.strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def _build_hutaba_post_emotion_reason(
    thread: dict[str, Any] | None,
    comment: str,
    target_reply: dict[str, Any] | None = None,
    dominant_emotion: str = "joy",
    max_chars: int = 55,
    *,
    url: str = "",
    board: str = "",
) -> str:
    """ふたばスレッド書き込み後の感情理由テキスト（LLM未使用・失敗時のフォールバック）。"""
    t = thread if isinstance(thread, dict) else {}
    raw_url = str(url or t.get("url") or "").lower()
    raw_board = str(board or t.get("board") or "").lower()
    srv = "may" if ("may.2chan.net" in raw_url or raw_board == "may") else "img"
    board_label = f"ふたば({srv})"

    topic = _extract_hutaba_thread_topic_snippet(t, max_len=20)
    comment_snip = _extract_hutaba_comment_snippet(comment, max_len=16)
    has_target = isinstance(target_reply, dict) and bool(str(target_reply.get("text") or "").strip())

    topic_part = f"「{topic}」" if topic else ""
    dom = str(dominant_emotion or "joy").strip().lower()

    if dom in ("joy", "fun", "happy"):
        if has_target:
            reason = f"{board_label}の{topic_part}スレでとしあきと掛け合いして楽しかった。"
        else:
            reason = f"{board_label}の{topic_part}スレに書き込んで楽しかった。"
    elif dom == "surprise":
        if has_target:
            reason = f"{board_label}の{topic_part}スレでとしあきの予想外な反応に驚いた。"
        else:
            reason = f"{board_label}の{topic_part}スレで意外な展開を見て驚いた。"
    elif dom in ("anticipation", "interest"):
        reason = f"{board_label}の{topic_part}スレの話題が気になって書き込んだ。"
    elif dom == "trust":
        reason = f"{board_label}の{topic_part}スレでとしあき達と共感し合った。"
    elif dom == "sadness":
        reason = f"{board_label}の{topic_part}スレで切ない話題にしんみりした。"
    elif dom in ("anger", "disgust", "fear"):
        reason = f"{board_label}の{topic_part}スレで少し戸惑いつつレスした。"
    else:  # neutral / その他
        if comment_snip:
            reason = f"{board_label}の{topic_part}スレで「{comment_snip}」とレスした。"
        else:
            reason = f"{board_label}の{topic_part}スレでとしあきと雑談した。"

    if max_chars and len(reason) > max_chars:
        reason = reason[: max_chars - 1] + "…"
    return reason


_SIBLING_PERSONA_SUFFIX_RE = re.compile(
    r"(?:なんですよ|ですよ|ですよね|ですね|ますよね|ますよ|ましたよ|ましたね|じゃないですか|"
    r"だけど|けれど|けど|さすがに|な話|だろな|だろうな|だろ|よな|よね|だな|かな|わね|わよ|"
    r"私は|私も|俺は|俺も|自分は|自分も|[…!！?？。、,.・\s])+"
)
_SIBLING_SYNONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("落ちて", "下がって"),
    ("落ちた", "下がった"),
    ("落ちる", "下がる"),
    ("暴落して", "下がって"),
)


def _normalize_for_sibling_plagiarism_check(text: Any) -> str:
    """他レス盗用・つぎはぎ判定用に、一人称・語尾・記号・定型フィラーを除去して正規化する。"""
    s = _strip_hutaba_quote_lines(text)
    s = re.sub(r">>?\s*\d+", "", s)
    s = unicodedata.normalize("NFKC", s).lower()
    for src, dst in _SIBLING_SYNONYM_PAIRS:
        s = s.replace(src, dst)
    # 句読点・記号・空白の除去前に、文頭の一人称や文末の定型語尾を境界付きで除去する
    s = re.sub(
        r"(?:^|(?<=[\s、。！？!?…・]))(?:わたし|私|あたし|僕|ぼく|俺|おれ|自分|うち)(?:は|も|が|の|的に|と)?",
        "",
        s,
    )
    s = re.sub(
        r"(?:です(?:ね|よ|よね|か)?|ます(?:ね|よ|よね|か)?|だ(?:ね|よ|な|よね|なぁ|なあ)|"
        r"ですよ(?:ね)?|ますよ(?:ね)?|かも(?:しれません|しれない|ですね|です)?|"
        r"かな(?:ぁ|あ)?|だよな(?:ぁ|あ)?|ですねぇ|ますねぇ|ちゃう(?:よね|ね|な)?|"
        r"ちゃいます(?:よね|ね|よ)?|ん(?:だよ|だな|ですね|ですよ|だよね))(?=[\s、。！？!?…・]*$)",
        "",
        s,
    )
    s = re.sub(r"[\s\u3000、。，．・…‥!！?？「」『』（）()［］\[\]〜~ー―\-:;\"'`]+", "", s)
    return s


_CHIMERA_GENERIC_TERMS = {
    "スレ", "レス", "スレ画", "画像", "自分", "相手", "本当", "今回", "最近",
    "前作", "今作", "作品", "話題", "内容", "感じ", "場合", "必要", "可能",
}

_CHIMERA_TERM_RE = re.compile(r"[ァ-ヶー]{2,}|[一-龯]{2,}")


def _extract_chimera_content_terms(text: Any) -> list[str]:
    """キメラ検出用に、カタカナ連続（2文字以上）と漢字連続（2文字以上）を固有語候補として抽出する。"""
    raw = _extract_hutaba_non_quote_body(text)
    terms: list[str] = []
    for m in _CHIMERA_TERM_RE.finditer(raw):
        val = m.group(0)
        if val in _CHIMERA_GENERIC_TERMS or val in _HUTABA_TOPIC_STOPWORDS:
            continue
        if val not in terms:
            terms.append(val)
    return terms


def _parse_hutaba_post_no_int(rep: dict[str, Any]) -> int:
    """レス辞書から投稿番号を安全な整数として取り出す。"""
    raw_post_no = str(rep.get("post_no") or "").strip()
    if raw_post_no.isdigit():
        return int(raw_post_no)
    norm_post_no = _normalize_img_post_no(raw_post_no) if raw_post_no else ""
    if norm_post_no.isdigit():
        return int(norm_post_no)
    raw_number = rep.get("number")
    if isinstance(raw_number, int):
        return raw_number
    raw_number_str = str(raw_number or "").strip()
    return int(raw_number_str) if raw_number_str.isdigit() else 0


def find_chimera_sentences(
    comment_text: Any,
    *,
    target_text: str,
    op_text: str,
    sibling_replies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """対象レス・OPに無い固有語を、単一の兄弟レスから2語以上借りて構成された文を検出する。

    戻り値は [{"sentence": str, "matched_post_no": int, "shared_terms": list[str]}]。
    対象レス・OPに固有語が1つでも含まれる文は「対象に錨打ちされている」として対象外。
    """
    cleaned = _extract_hutaba_non_quote_body(comment_text)
    if not cleaned:
        return []
    anchor_text = f"{target_text or ''}\n{op_text or ''}"
    sentences = [
        s.strip()
        for s in re.split(r"[。！？!?…\n]+", cleaned)
        if s.strip()
    ]
    hits: list[dict[str, Any]] = []
    for sentence in sentences:
        sentence_terms = _extract_chimera_content_terms(sentence)
        if len(sentence_terms) < 2:
            continue
        if any(term in anchor_text for term in sentence_terms):
            continue
        for rep in sibling_replies:
            if not isinstance(rep, dict):
                continue
            rep_body = str(rep.get("body") or rep.get("text") or "")
            if not rep_body:
                continue
            shared = [t for t in sentence_terms if t in rep_body]
            if len(shared) >= 2:
                hits.append(
                    {
                        "sentence": sentence,
                        "matched_post_no": _parse_hutaba_post_no_int(rep),
                        "shared_terms": shared,
                    }
                )
                break
    return hits


_IMG2CHAN_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。．！？!?…‥\n])")


def _normalize_img2chan_sentence(sentence: str) -> str:
    """文単位の比較・空断片判定用に NFKC 正規化・小文字化し、空白・句読点・記号を除去する。"""
    normalized = unicodedata.normalize("NFKC", str(sentence or "")).lower()
    return "".join(
        ch
        for ch in normalized
        if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )


def _split_img2chan_sentences(text: str) -> list[str]:
    """引用行を除いた本文を文単位（句読点・感嘆符・疑問符・省略記号・改行）に分割し、記号のみの断片を除外する。"""
    body = _extract_hutaba_non_quote_body(text)
    if not body:
        return []
    sentences: list[str] = []
    for raw_part in _IMG2CHAN_SENTENCE_SPLIT_RE.split(body):
        part = raw_part.strip()
        if not part:
            continue
        if not _normalize_img2chan_sentence(part):
            continue
        sentences.append(part)
    return sentences


def _img2chan_sentence_count(text: str) -> int:
    """投稿本文の文数を返す（記号のみの断片は数えない）。"""
    return len(_split_img2chan_sentences(text))


def _img2chan_comment_overlaps_siblings(
    comment: str,
    sibling_texts: Sequence[str],
) -> bool:
    """候補本文と既出子レス群を文単位で突き合わせ、語尾変えパクり相当の重複があれば True を返す。"""
    if not comment or not sibling_texts:
        return False

    candidate_norms: list[str] = []
    for sent in _split_img2chan_sentences(comment):
        norm = _normalize_img2chan_sentence(sent)
        if len(norm) >= 8:
            candidate_norms.append(norm)
    if not candidate_norms:
        return False

    sibling_norms: list[str] = []
    for sib in sibling_texts:
        if not sib:
            continue
        for sent in _split_img2chan_sentences(str(sib)):
            norm = _normalize_img2chan_sentence(sent)
            if norm:
                sibling_norms.append(norm)
    if not sibling_norms:
        return False

    for c_norm in candidate_norms:
        for s_norm in sibling_norms:
            matcher = difflib.SequenceMatcher(None, c_norm, s_norm, autojunk=False)
            if matcher.ratio() >= 0.70:
                return True
            longest = matcher.find_longest_match(0, len(c_norm), 0, len(s_norm))
            if longest.size >= 10:
                return True
    return False


def _extract_hutaba_post_no_str(post: dict[str, Any]) -> str:
    raw = str(post.get("post_no") or "").strip()
    if not raw:
        return ""
    norm = _normalize_img_post_no(raw)
    return norm if norm else re.sub(r"^No\.", "", raw, flags=re.IGNORECASE).strip()


def _safe_hutaba_reply_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_reply_to_hutaba_target(
    thread: dict[str, Any],
    candidate: dict[str, Any],
    *,
    target_p_no: str,
    target_r_no: int,
    target_first_line: str,
    find_quoted_fn: Any,
) -> bool:
    text = str(candidate.get("text") or candidate.get("body") or "").strip()
    if not text:
        return False
    all_quote_lines = [
        ln.strip().lstrip(">").strip()
        for ln in text.splitlines()
        if ln.strip().startswith(">")
    ]
    direct_quote_lines = [
        ln.strip()[1:].strip()
        for ln in text.splitlines()
        if ln.strip().startswith(">") and not ln.strip().startswith(">>")
    ]
    if target_p_no and any(
        q == target_p_no or q.lower() == f"no.{target_p_no.lower()}"
        for q in all_quote_lines
    ):
        return True
    if target_r_no > 0 and any(q == str(target_r_no) for q in all_quote_lines):
        return True
    if len(target_first_line) >= 4 and any(
        target_first_line in q for q in direct_quote_lines
    ):
        return True
    if callable(find_quoted_fn) and all_quote_lines:
        with contextlib.suppress(Exception):
            parents = find_quoted_fn(thread, candidate, max_targets=1)
            if parents and isinstance(parents[0], dict):
                pp_no = _extract_hutaba_post_no_str(parents[0])
                pr_no = _safe_hutaba_reply_int(parents[0].get("number"))
                if (target_p_no and pp_no == target_p_no) or (
                    target_r_no > 0 and pr_no == target_r_no
                ):
                    return True
    return False


def _collect_sibling_reply_texts(
    thread: dict[str, Any] | None,
    target_reply: dict[str, Any] | None,
) -> list[str]:
    """対象レスへの返信（子レス）から引用行を除いた本文を最大5件まで収集する。"""
    if not isinstance(thread, dict) or not isinstance(target_reply, dict):
        return []
    raw_posts = thread.get("all_replies") or thread.get("replies") or thread.get("posts") or []
    if not isinstance(raw_posts, list) or not raw_posts:
        return []
    posts = [p for p in raw_posts if isinstance(p, dict)]
    if not posts:
        return []

    target_p_no = _extract_hutaba_post_no_str(target_reply)
    target_r_no = _safe_hutaba_reply_int(target_reply.get("number"))
    target_text = str(target_reply.get("text") or target_reply.get("body") or "").strip()
    target_plain_lines = [
        ln.strip()
        for ln in target_text.splitlines()
        if ln.strip() and not ln.strip().startswith(">")
    ]
    target_first_line = target_plain_lines[0][:24] if target_plain_lines else ""

    target_idx = -1
    for idx, p in enumerate(posts):
        p_no = _extract_hutaba_post_no_str(p)
        p_r_no = _safe_hutaba_reply_int(p.get("number"))
        if (target_p_no and p_no == target_p_no) or (
            target_r_no > 0 and p_r_no == target_r_no
        ):
            target_idx = idx
            break

    find_quoted_fn = getattr(_get_img2chan_helper(), "find_img_thread_quoted_posts", None)
    search_replies = posts[target_idx + 1 :] if target_idx >= 0 else posts
    seen_keys: set[str] = set()
    sibling_texts: list[str] = []

    for p in search_replies:
        if p.get("is_op") or p is target_reply:
            continue
        p_no = _extract_hutaba_post_no_str(p)
        p_r_no = _safe_hutaba_reply_int(p.get("number"))
        if (target_p_no and p_no == target_p_no) or (
            target_r_no > 0 and p_r_no == target_r_no
        ):
            continue
        seen_key = f"p:{p_no}" if p_no else (f"r:{p_r_no}" if p_r_no > 0 else "")
        if seen_key and seen_key in seen_keys:
            continue
        if not _is_reply_to_hutaba_target(
            thread,
            p,
            target_p_no=target_p_no,
            target_r_no=target_r_no,
            target_first_line=target_first_line,
            find_quoted_fn=find_quoted_fn,
        ):
            continue
        body = _extract_hutaba_non_quote_body(p.get("text") or p.get("body") or "")
        if not body:
            continue
        if seen_key:
            seen_keys.add(seen_key)
        sibling_texts.append(body)
        if len(sibling_texts) >= 5:
            break
    return sibling_texts


def _sanitize_hutaba_reply_text(
    text: str,
    *,
    max_lines: int = 4,
    max_chars: int = 180,
) -> str:
    """ふたば（img）向けレス文のサニタイズ。
    思考タグ除去、相手呼称（特定個人名）排除、Shift_JIS互換性維持、文字数制限。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    # JSONのcommentフィールド内にLLMが誤って「>対象レス本文」の引用行を含めた場合、
    # extract_first_user_facing_reply が先頭の「>」だけを剥がして本文に混入させるのを防ぐため、
    # 他に通常の本文行がある場合は非アンカーの引用行（>数字 以外）を事前に除去する。
    raw_lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    anchor_lines: list[str] = []
    body_lines: list[str] = []
    for ln in raw_lines:
        if re.match(r"^[>＞]+\s*\d+", ln):
            anchor_lines.append(ln)
        elif not re.match(r"^[>＞]", ln):
            body_lines.append(ln)

    if body_lines:
        raw_to_clean = "\n".join(body_lines)
    else:
        raw_to_clean = "\n".join(raw_lines)

    raw_to_clean = _clean_hutaba_thought_tags(raw_to_clean)
    cleaned_body = extract_first_user_facing_reply(raw_to_clean) or sanitize_generated_reply(raw_to_clean)
    cleaned_body = cleaned_body.replace("\r\n", "\n").replace("\r", "\n")

    # アンカー引用行があれば先頭に復元
    if anchor_lines and cleaned_body:
        raw = "\n".join([*anchor_lines, cleaned_body])
    elif anchor_lines:
        raw = "\n".join(anchor_lines)
    else:
        raw = cleaned_body

    # 相手呼称の抑止（「○○さん、」「○○さん」などの捏造個人名を削除）
    raw = _HUTABA_HONORIFIC_NAME_PATTERN.sub("", raw)

    # Bot名や自己紹介的な名乗りの除去
    display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if display_name:
        raw = re.sub(rf"(?:私|わたし|僕|ボク)は{re.escape(display_name)}[ですだ]", "", raw)

    # 自分への呼びかけ（「コイト、…」等）の除去
    if _strip_self_name_vocative is not None and _is_self_name_guard_enabled is not None and _collect_self_names is not None:
        try:
            if _is_self_name_guard_enabled():
                _stripped = _strip_self_name_vocative(raw, _collect_self_names())
                if _stripped != raw:
                    log.info("[hutaba] self-name vocative stripped")
                    raw = _stripped
        except Exception:
            pass

    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    # 文末等の「…！」が1レス内で複数回連発されるのを抑止
    # 最終行が短い感嘆のみの行（本文8文字以内かつ「…！」終わり）の場合は最終行を残し先行側を「。」へ正規化、
    # それ以外は最初の1回のみ維持し2回目以降を「。」へ正規化する。
    ellipsis_bang_pattern = re.compile(r"(?:…+|\.{2,})[！!]+")
    matches = list(ellipsis_bang_pattern.finditer(raw))
    if len(matches) >= 2:
        raw_split_lines = raw.split("\n")
        keep_index = 0
        if len(raw_split_lines) >= 2:
            last_line = raw_split_lines[-1].strip()
            last_line_matches = list(ellipsis_bang_pattern.finditer(last_line))
            if (
                last_line
                and not last_line.startswith((">", "＞"))
                and len(last_line_matches) == 1
                and last_line_matches[0].end() == len(last_line)
            ):
                last_line_body = last_line[: last_line_matches[0].start()].strip()
                if 0 < len(last_line_body) <= 8 and not re.search(r"[。！？!?]", last_line_body):
                    keep_index = len(matches) - 1

        current_index = 0

        def _replace_extra_ellipsis_bang(match: re.Match[str]) -> str:
            nonlocal current_index
            idx = current_index
            current_index += 1
            if idx == keep_index:
                return match.group(0)
            return "。"

        raw = ellipsis_bang_pattern.sub(_replace_extra_ellipsis_bang, raw)
        raw = re.sub(r"。{2,}", "。", raw)

    try:
        raw.encode("cp932")
    except UnicodeEncodeError:
        raw = raw.encode("cp932", errors="ignore").decode("cp932")

    helper = _get_img2chan_helper()
    if helper is not None and hasattr(helper, "sanitize_img_post_text"):
        raw = getattr(helper, "sanitize_img_post_text")(raw, max_chars=max_chars)
    else:
        raw = truncate_text(raw, max_chars).strip()

    return raw


_HUTABA_ANGER_OR_DENIAL_PATTERNS = [
    r"(?:ちがう|違う|そうじゃない|言ってない|言っとらん)",
    r"(?:いい加減にしろ|ふざけんな|黙れ|うぜえ|ウザい|消えろ)",
    r"(?:なんなん|何言ってんだ|会話にならん|会話になってない)",
    r"[！!]{3,}",
]


def _looks_like_hutaba_anger_or_denial(text: str) -> bool:
    raw = str(text or "")
    return any(re.search(p, raw) for p in _HUTABA_ANGER_OR_DENIAL_PATTERNS)


class ParrotReason(str):
    """オウム返し・盗用検知理由を表す文字列型。観測用の詳細メタデータを保持する。"""
    matched_sibling_post_nos: list[int]
    longest_common_subsequence_len: int

    def __new__(
        cls,
        reason: str,
        *,
        matched_sibling_post_nos: list[int] | None = None,
        longest_common_subsequence_len: int = 0,
    ) -> ParrotReason:
        obj = super().__new__(cls, reason)
        obj.matched_sibling_post_nos = list(matched_sibling_post_nos or [])
        obj.longest_common_subsequence_len = int(longest_common_subsequence_len)
        return obj


def _img2chan_should_skip_salvaged(salvaged: bool, *, enabled: bool) -> bool:
    """切断サルベージ本文を見送るべきかを返す（§39）。

    自発投稿・返信タスクを問わず、salvaged かつ有効フラグが True の場合は見送る。
    """
    return bool(salvaged) and bool(enabled)


def _img2chan_output_order_violated(parsed: Any) -> bool:
    """生成 JSON のキー順序が契約（thought → quote_post_numbers → comment）通りかを返す。

    thought より前に comment が出現していたら順序違反。thought の推論が
    comment の生成に先行していない候補は、Step B/C の禁止事項が本文に
    効いていないため採用しない。キーが欠けている場合は違反としない
    （欠落は既存の別チェックが扱う）。
    """
    if not isinstance(parsed, dict):
        return False
    keys = [str(k) for k in parsed.keys()]
    if "thought" in keys and "comment" in keys:
        return keys.index("comment") < keys.index("thought")
    return False


def _img2chan_order_violation_action(parsed: Any, *, adopt_enabled: bool) -> str:
    """生成順序違反時の候補の扱いを返す。戻り値は "ok" / "adopt" / "skip"。

    - 順序違反でなければ "ok"（従来フローへ）。
    - 順序違反でも thought が実質的に含まれていれば "adopt"（緩和採用）。
    - thought が無い・空・短すぎる場合、または緩和が無効な場合は "skip"。
    """
    if not _img2chan_output_order_violated(parsed):
        return "ok"
    if not adopt_enabled:
        return "skip"
    thought = parsed.get("thought") if isinstance(parsed, dict) else None
    if isinstance(thought, str) and len(thought.strip()) >= 10:
        return "adopt"
    return "skip"


def _img2chan_matched_all_own(matched: object, own_post_nos: object) -> bool:
    """オウム返し判定で一致した投稿番号がすべて自分の投稿かを返す（§39）。"""
    if not isinstance(matched, Iterable) or isinstance(matched, (str, bytes)):
        return False
    try:
        matched_strs = [str(x).strip() for x in matched if str(x).strip()]
    except TypeError:
        return False
    if not matched_strs:
        return False
    if not isinstance(own_post_nos, Iterable) or isinstance(own_post_nos, (str, bytes)):
        return False
    try:
        own_set = {str(x).strip() for x in own_post_nos if str(x).strip()}
    except TypeError:
        return False
    if not own_set:
        return False
    return all(no in own_set for no in matched_strs)


_OPENING_HOOK_ECHO_SPLIT_RE = re.compile(r"[、。，．！？!?\n]")
_OPENING_HOOK_ECHO_LEADING_DELIM_RE = re.compile(r"^[\s、。，．！？!?\n]+")
_OPENING_HOOK_ECHO_ANCHOR_PREFIX_RE = re.compile(r"^[>＞]+\s*\d+\s*")


def _extract_opening_hook_echo_body(text: str, *, fallback_raw: bool = False) -> str:
    raw = str(text or "")
    lines: list[str] = []
    for raw_line in raw.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _OPENING_HOOK_ECHO_ANCHOR_PREFIX_RE.match(stripped):
            stripped = _OPENING_HOOK_ECHO_ANCHOR_PREFIX_RE.sub("", stripped).strip()
            if stripped:
                lines.append(stripped)
            continue
        if stripped.startswith((">", "＞")):
            continue
        lines.append(stripped)
    body = "\n".join(lines).strip()
    if not body and fallback_raw:
        return raw.strip()
    return body


def _normalize_opening_hook_echo_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] in ("L", "N"))


def _img2chan_opening_hook_echo(comment: str, target_text: str) -> bool:
    """本文冒頭（最初の読点・句点まで、最大15文字）に対象レスの連続4文字以上が含まれるか判定する（§114）。"""
    comment_body = _extract_opening_hook_echo_body(comment, fallback_raw=False)
    target_body = _extract_opening_hook_echo_body(target_text, fallback_raw=True)
    if not comment_body or not target_body:
        return False

    trimmed_comment = _OPENING_HOOK_ECHO_LEADING_DELIM_RE.sub("", comment_body)
    if not trimmed_comment:
        return False
    first_clause = _OPENING_HOOK_ECHO_SPLIT_RE.split(trimmed_comment, maxsplit=1)[0]

    opening_norm = _normalize_opening_hook_echo_text(first_clause)[:15]
    target_norm = _normalize_opening_hook_echo_text(target_body)
    if len(opening_norm) < 4 or len(target_norm) < 4:
        return False

    for idx in range(len(opening_norm) - 3):
        if opening_norm[idx : idx + 4] in target_norm:
            return True
    return False
