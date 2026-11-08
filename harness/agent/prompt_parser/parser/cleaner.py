"""テキストの正規化、不要記号・Markdown装飾除去、一人称や感謝表現の補正処理。"""
from __future__ import annotations

import difflib
import re


def strip_markdown_artifacts(text: str) -> str:
    """Markdown装飾（太字、斜体、リンク、コード、引用記号、見出し等）を除去して平文にする。"""
    value = str(text or "").strip()
    if not value:
        return ""

    value = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", lambda m: f"{m.group(1).strip()}: {m.group(2)}".strip(": "), value)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", lambda m: f"{m.group(1).strip()}: {m.group(2)}", value)
    value = re.sub(r"`([^`\n]+)`", r"\1", value)
    value = re.sub(r"(\*\*|__)(.*?)\1", r"\2", value)
    value = re.sub(r"(~~)(.*?)\1", r"\2", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", value)

    lines: list[str] = []
    for line in value.splitlines():
        s = line.rstrip()
        if re.fullmatch(r"\s*(?:[-*_]\s*){3,}", s):
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
        s = re.sub(r"^\s{0,3}>\s?", "", s)
        s = re.sub(r"^\s*(?:[-*+・]|[0-9０-９]+[.)．、])\s+", "", s)
        lines.append(s.rstrip())

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_leading_list_marker(text: str) -> tuple[str, bool]:
    """箇条書き記号（-, *, +, ・, 1. など）が先頭にあれば除去する。"""
    value = str(text or "").strip()
    match = re.match(r"^(?:[-*+・]|[0-9０-９]+[.)．、])\s*(.+)$", value)
    if not match:
        return value, False
    return str(match.group(1) or "").strip(), True


def _strip_self_addressed_thanks(text: str) -> str:
    """文末やお礼表現で『ありがとう、Bot名』のように自分自身の名前で相手に感謝・呼びかけている誤りを修復する。"""
    t = str(text or "")
    if not t:
        return ""
    from ollama_bot.common.bot_identity import _extract_character_names_from_config
    names = _extract_character_names_from_config()
    if names:
        escaped = "|".join(re.escape(n) for n in names if n)
        pattern = rf"(ありがとう(?:ございます)?|サンキュー|感謝(?:します|いたします)?)[、,\s]+(?:{escaped})(?:ちゃん|さん)?([!！…\.\?？~〜\s]*)$"
    else:
        pattern = r"(ありがとう(?:ございます)?|サンキュー|感謝(?:します|いたします)?)[、,\s]+(?:アシスタント|AI|Bot)(?:ちゃん|さん)?([!！…\.\?？~〜\s]*)$"
    t = re.sub(pattern, r"\1\2", t, flags=re.MULTILINE | re.IGNORECASE)
    return t


def strip_bot_self_mentions(
    text: str,
    *,
    bot_user_id: int | None = None,
    replacement_name: str = "",
) -> str:
    """LLMが誤って出力したBot自身へのメンション（<@bot_user_id> や <@!bot_user_id>）を除去または置換する。

    相手の名前（replacement_name）がある場合は「{replacement_name}さん」に置換し、
    ない場合はメンションを除去して自然な文章にする。
    """
    t = str(text or "")
    if not t:
        return ""

    bot_ids: set[int] = set()
    if bot_user_id is not None and int(bot_user_id) > 0:
        bot_ids.add(int(bot_user_id))

    try:
        from lib.config_utils import cfg
        configured_bot_id = cfg("DISCORD_BOT_USER_ID", None)
        if configured_bot_id:
            bot_ids.add(int(configured_bot_id))
    except Exception:
        pass

    clean_name = str(replacement_name or "").strip()
    name_with_san = ""
    if clean_name:
        if clean_name.endswith(("さん", "くん", "君", "ちゃん", "氏", "先生")):
            name_with_san = clean_name
        else:
            name_with_san = f"{clean_name}さん"

    modified = False
    for bid in bot_ids:
        mention_pat = rf"<@!?{bid}>"
        if name_with_san:
            # 「<@bot_id>さん」等も考慮して置換
            new_t, count = re.subn(rf"{mention_pat}(?:さん|ちゃん|くん|君)?", name_with_san, t)
            if count > 0:
                t = new_t
                modified = True
        else:
            # 相手名がない場合はメンションを除去
            # 直後に助詞「が」「は」等がある場合は主語ごと除去されるのを防ぐため、メンション部分のみ除去
            new_t, count = re.subn(rf"{mention_pat}(?:さん|ちゃん|くん|君)?\s*", "", t)
            if count > 0:
                t = new_t
                modified = True

    # 相手名なしで文頭のメンションを除去した結果、助詞のみが文頭に残った場合の微調整
    # （例: 「<@123>が調べたいのは」 -> 「が調べたいのは」 -> 「調べたいのは」）
    if modified and not name_with_san:
        if not re.match(r"^は[あぁ!?！？]", t):
            t = re.sub(r"^[がはのもにへよりからでって](?=[ぁ-んァ-ヶ一-龠A-Za-z0-9])\s*", "", t)
    return t


_BBS_SLANG_CALLING_PATTERNS = ("「」ちゃん", "「」", "としあき")

# 他ユーザー名による「呼びかけ誤認」とみなす末尾境界（キャプチャグループ1つ）。
# 純粋な呼びかけ・主題提示のみを対象とし、第三者への言及となる連体修飾・格助詞句
# （「〇〇への」「〇〇さんの発言」「〇〇について」「〇〇に関する」「〇〇にとって」「〇〇より」等）は
# 置換対象から除外する（話題の第三者への正しい言及を相手名へ過剰置換する誤爆の防止）。
#   1) 呼びかけ・主語の助詞（は/も/ね/よ/って）で、直後に格助詞（の/へ 等）が続かない場合
#   2) 直後に読点・約物・空白が続く場合（空白を挟んだ「への」等の第三者言及は除外）
#   3) 文末
_PARTNER_CALLING_TAIL = (
    r"("
    r"(?:[はもね](?![のへ])|よ(?![りのへ])|って(?!いう|言う|の))"
    r"|[、，!！…\.\?？~〜\s]+(?!\s*(?:への|について|に関する|にとって|に対する))"
    r"|$"
    r")"
)


# 第三者言及・引用・客観描写の後続パターン
_THIRD_PARTY_FOLLOWING_PATTERN = re.compile(
    r"^\s*(?:"
    r"[「『]"
    r"|って(?:言|話|書|叫|述|伝|つぶや|思|主張|投稿|リプ)"
    r"|と(?:言|話|書|叫|述|伝|つぶや|思|主張|投稿)"
    r"|らしい"
    r"|によると"
    r"|によれば"
    r"|の(?:発言|意見|言った|言っている|言葉|投稿|書き込み|主張|意図)"
    r"|自身"
    r"|が(?:言|話|書|叫|述|伝|主張)"
    r"|(?:が|は|も|を|に)?(?:言っ|言って|話して|書いて|述べて|主張して)"
    r")"
)

# 助詞（は/も/って 等）の後の同一文節内に含まれる客観描写・引用・伝聞パターン
_THIRD_PARTY_IN_CLAUSE_PATTERN = re.compile(
    r"(?:"
    r"[「『]"
    r"|って(?:言|話|書|叫|述|伝|つぶや|思|主張|投稿|リプ)"
    r"|と(?:言|話|書|叫|述|伝|つぶや|思|主張|投稿)"
    r"|らしい"
    r"|によると"
    r"|によれば"
    r"|の(?:発言|意見|言った|言っている|言葉|投稿|書き込み|主張|意図)"
    r")"
)


def _is_third_party_mention(tail: str, rest: str) -> bool:
    """名前の直後の助詞（tail）および後続テキスト（rest）から、
    相手への直接の呼びかけではなく第三者の言及・引用・対比であるかを判定する。
    """
    # 1. 後続が引用符や客観描写・伝聞で直接始まる場合は無条件で第三者言及
    if _THIRD_PARTY_FOLLOWING_PATTERN.search(rest):
        return True

    # 2. 助詞が「は」「も」「って」などの場合、同一節内（文境界まで）に客観描写・引用・伝聞が含まれていれば第三者言及
    clean_tail = tail.strip()
    if clean_tail.startswith(("は", "も", "って")):
        end_idx = len(rest)
        for delim in ("。", "！", "!", "？", "?", "\n"):
            pos = rest.find(delim)
            if pos != -1 and pos < end_idx:
                end_idx = pos
        clause = rest[:end_idx]
        if _THIRD_PARTY_IN_CLAUSE_PATTERN.search(clause):
            return True

    return False


def sanitize_partner_misaddressing(
    text: str,
    *,
    target_name: str = "",
    other_names: list[str] | set[str] | None = None,
) -> str:
    """直接対話（メンションや返信）において、相手（target_name）以外の会話履歴内のユーザー名（other_names）や
    掲示板スラング（「」ちゃん、としあき等）で相手に呼びかけてしまった誤りをサニタイズする。
    """
    t = str(text or "")
    clean_target = str(target_name or "").strip()
    if not t or not clean_target:
        return t

    target_san = f"{clean_target}さん" if not clean_target.endswith(("さん", "くん", "君", "ちゃん", "氏", "先生", "たん")) else clean_target

    # 1. 掲示板スラング呼称（「」ちゃん、としあき等）による相手への呼びかけ・主語誤認の置換
    bbs_pattern_str = "|".join(re.escape(n) for n in _BBS_SLANG_CALLING_PATTERNS)

    def _repl_bbs_with_sep(match: re.Match) -> str:
        sep = match.group(1) or ""
        tail = match.group(2) or ""
        return f"{sep}{target_san}{tail}"

    t = re.sub(
        rf"(^|[、，\sはがのをにへとで])(?:{bbs_pattern_str})(?:さん|ちゃん)?([はもってへのに]|[、，!！…\.\?？~〜\s]+)",
        _repl_bbs_with_sep,
        t,
        flags=re.IGNORECASE,
    )

    # 2. 会話履歴内の他ユーザー名（other_names）による呼びかけ誤認の置換
    if not other_names:
        return t

    valid_other_names = [str(n).strip() for n in other_names if str(n).strip() and str(n).strip() != clean_target]
    if not valid_other_names:
        return t

    name_candidates: set[str] = set()
    for n in valid_other_names:
        name_candidates.add(n)
        stripped = re.sub(r"(さん|ちゃん|くん|君|氏|先生|たん)$", "", n).strip()
        if stripped and stripped != clean_target:
            name_candidates.add(stripped)

    sorted_other_names = sorted(name_candidates, key=len, reverse=True)
    other_pattern_str = "|".join(re.escape(n) for n in sorted_other_names)

    _sep_prefix = r"[、，\s!！?？…\.\n~〜\(\)（）「」『』]"

    def _repl_with_comma(match: re.Match) -> str:
        sep = match.group(1) or ""
        tail = match.group(2) or ""
        rest = match.string[match.end():]
        if _is_third_party_mention(tail, rest):
            return match.group(0)
        if target_san:
            return f"{sep}{target_san}{tail}"
        return tail

    def _repl_start(match: re.Match) -> str:
        tail = match.group(2) or ""
        rest = match.string[match.end():]
        if _is_third_party_mention(tail, rest):
            return match.group(0)
        if target_san:
            return f"{target_san}{tail}"
        return ""

    t = re.sub(
        rf"^({other_pattern_str})(?:さん|ちゃん|くん|君)?{_PARTNER_CALLING_TAIL}",
        _repl_start,
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        rf"({_sep_prefix})(?:{other_pattern_str})(?:さん|ちゃん|くん|君)?{_PARTNER_CALLING_TAIL}",
        _repl_with_comma,
        t,
        flags=re.IGNORECASE,
    )
    return t


def _normalize_unintended_watashi(text: str) -> str:
    """動揺時や自己弁護時に一人称が『私』にブレてしまう誤りを設定一人称に修復する。"""
    t = str(text or "")
    if not t:
        return ""
    from lib.config_utils import cfg
    should_replace = cfg("CHARACTER_DEVIATION_AUTO_WATASHI_REPLACE", False)
    target_fp = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    if not should_replace or not target_fp:
        return t
    pattern = r"(^|[\s、。！？!?.…「」『』\n])私([、，はがのもにへよりからでって]|だって|なんか|なんて|自身)"
    return re.sub(pattern, rf"\g<1>{target_fp}\2", t)


def strip_unprompted_nerd_emoji(user_text: str, reply: str) -> str:
    """ユーザー発言に 🤓 が含まれていない場合、AI返答に含まれる 🤓 を除去する。"""
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r:
        return ""
    if "🤓" in u:
        return r
    if "🤓" in r:
        r = r.replace("🤓", "").strip()
    return r


def strip_trailing_reasoning_notes(text: str) -> str:
    """文末に付着した英語の検証メモや思考メモ（(Matches exactly), (verified) 等）を除去する。"""
    t = str(text or "").strip()
    if not t:
        return ""
    # 1. (Matches exactly), (Matches), (verified), (passed), (done), (OK) 等の明示的な思考メモ
    t = re.sub(
        r"\s*\(?(?:Matches(?:\s+exactly)?|Verified|Passed|Proceeds|Output matches)\)?\.?$",
        "",
        t,
        flags=re.IGNORECASE,
    ).strip()
    # 2. クォート閉じの後ろ、または文末の英字括弧メモ: \([A-Za-z\s_-]{2,40}\)\.?$
    t = re.sub(r"\s*\([A-Za-z\s_-]{2,40}\)\.?$", "", t).strip()
    # 3. クォートやシングルクォートの残骸（例: ' や "）が末尾に残っている場合をトリム
    t = re.sub(r"['\"]+$", "", t).strip()
    return t


from lib.text_utils import (
    extract_effective_first_clause,
    normalize_compare_text as _normalize_compare_text,
    strip_leading_interjections,
)


def _normalize_topic_echo_text(text: str) -> str:
    """話題オウム返し判定用に表記ゆれや助詞を除去して正規化する。"""
    t = _normalize_compare_text(text)
    t = re.sub(r"(?:ごはん|ご飯|御飯)", "飯", t)
    t = re.sub(r"[をがはのにへと]", "", t)
    return t


def _opening_echo_fragment(text: str) -> str:
    """文頭のオウム返し検出用の冒頭句を抽出・正規化する。"""
    head = extract_effective_first_clause(text)
    head = re.sub(
        r"(?:だよな|だろ|だよ|なのか|なの|だと|だな|って|とは|という|か|ね|な|だ)$",
        "",
        head,
    ).strip()
    return _normalize_topic_echo_text(head)


_KAOMOJI_PATTERN = re.compile(
    r"[＼\\/／ง*＊]*[\(（][^\n()（）]{1,35}[\)）][＼\\/／ง*＊]*"
)

_KAOMOJI_PARTS_PATTERN = re.compile(
    r"[\^＾○〇〜~\*＊´｀ﾟ・ωД∀дㅂര˃˂ഃ_＿TvVoO0><≧≦qp\+＋xX×ェ3ε;；!！?？|｜\-ーｰっつノﾉ꒳｡°ᴗ˘◡◠]"
)

_NON_KAOMOJI_TEXT_PATTERN = re.compile(
    r"^[\(（](?:月|火|水|木|金|土|日|株|有|笑|爆|泣|汗|怒|仮|終|注|例|参考|前|後|上|中|下|同|祝)[\)）]$"
)


def is_kaomoji(candidate: str) -> bool:
    """文字列が特徴的な顔文字（パーツ組み合わせ顔文字）であるかを判定する。"""
    c = candidate.strip()
    if not c or len(c) < 3:
        return False
    if _NON_KAOMOJI_TEXT_PATTERN.match(c):
        return False
    inner = re.sub(r"^[＼\\/／ง*＊]*[\(（]|[\)）][＼\\/／ง*＊]*$", "", c).strip()
    if not inner:
        return False
    # 日本語の漢字・ひらがな・カタカナ・読点だけで構成され、記号パーツが一切ない場合は顔文字ではない
    if re.fullmatch(r"[ぁ-んァ-ヶ一-龠\s、，。]+", inner):
        return False
    # 顔文字パーツ文字が含まれている、または非ASCII/非日本語記号が含まれている
    if _KAOMOJI_PARTS_PATTERN.search(inner):
        return True
    return False


def extract_kaomoji_candidates(text: str) -> list[str]:
    """テキストから特徴的な顔文字の候補を抽出してリストで返す。"""
    if not text:
        return []
    matches = _KAOMOJI_PATTERN.findall(text)
    results: list[str] = []
    seen: set[str] = set()
    for m in matches:
        m_str = m.strip()
        if m_str not in seen and is_kaomoji(m_str):
            seen.add(m_str)
            results.append(m_str)
    return results


def strip_mimicked_kaomoji(user_text: str, reply: str) -> str:
    """対象ユーザーの発言に含まれる特徴的な顔文字を抽出し、AI返答に模倣付着している場合に除去する。"""
    u = str(user_text or "").strip()
    r = str(reply or "").strip()
    if not u or not r:
        return r

    user_kaomojis = extract_kaomoji_candidates(u)
    if not user_kaomojis:
        return r

    try:
        from lib.config_utils import cfg
        allowed_kaomojis: set[str] = set(cfg("ALLOWED_KAOMOJI", []) or [])
    except Exception:
        allowed_kaomojis = set()

    for k in user_kaomojis:
        if k in allowed_kaomojis:
            continue
        if k in r:
            # 1. 文末に付着している場合
            r = re.sub(rf"[\s、，]*{re.escape(k)}[\s!！…\.\?？~〜]*$", "", r)
            # 2. 文中に残っている場合
            r = r.replace(k, "")

    # 連続空白の整形
    r = re.sub(r"[ \t]{2,}", " ", r)
    return r.strip()


def strip_hanging_tail_echo(text: str, user_text: str = "") -> str:
    """接続句の直後に相手の語尾（『さあ』『さ』等）がくっついて終わっている文末破綻を補正・除去する。"""
    t = str(text or "").strip()
    if not t:
        return ""

    # 1. なんですけど/んですけど ＋ 読点/空白 ＋ さあ/さ
    t = re.sub(
        r"([ぁ-んァ-ヶ一-龠A-Za-z0-9]+(?:なんですけど|んですけど))[、，\s]*(?:さあ|さ)[!！…\.\?？~〜\s]*$",
        r"\1ね…！",
        t,
    )
    # 2. なんだけど/だけど ＋ 読点/空白 ＋ さあ/さ
    t = re.sub(
        r"([ぁ-んァ-ヶ一-龠A-Za-z0-9]+(?:なんだけど|だけど))[、，\s]*(?:さあ|さ)[!！…\.\?？~〜\s]*$",
        r"\1ね…！",
        t,
    )
    # 3. ですが/だが/けれど/けれども/のに ＋ 読点/空白 ＋ さあ/さ
    t = re.sub(
        r"([ぁ-んァ-ヶ一-龠A-Za-z0-9]+(?:ですが|だが|けれど|けれども|のに))[、，\s]*(?:さあ|さ)[!！…\.\?？~〜\s]*$",
        r"\1…！",
        t,
    )
    # 4. 単独のぶら下がり「、さあ」「、さ」が文末に残っている場合
    t = re.sub(r"[、，\s]+(?:さあ|さ)[!！…\.\?？~〜\s]*$", "…！", t)
    return t.strip()


def strip_copied_previous_reply_segments(reply: str, previous_reply: str = "") -> str:
    """前回のBot返信から機械的にコピーされた段落・節（セルフエコー）を検出して除去する。"""
    rep = str(reply or "").strip()
    prev = str(previous_reply or "").strip()
    if not rep or not prev:
        return rep

    prev_norm = _normalize_compare_text(prev)
    if len(prev_norm) < 10:
        return rep

    # 段落（\n\n または \n）ごとに分割
    is_double_newline = "\n\n" in rep
    delimiter = "\n\n" if is_double_newline else "\n"
    paragraphs = [p.strip() for p in rep.split(delimiter) if p.strip()]
    if len(paragraphs) <= 1:
        lines = [line.strip() for line in rep.splitlines() if line.strip()]
        if len(lines) <= 1:
            return rep
        paragraphs = lines
        delimiter = "\n"

    prev_paragraphs = [p.strip() for p in re.split(r"\n+", prev) if p.strip()]
    prev_p_norms = [_normalize_compare_text(p) for p in prev_paragraphs if len(_normalize_compare_text(p)) >= 10]

    kept: list[str] = []
    removed_any = False
    for p in paragraphs:
        p_norm = _normalize_compare_text(p)
        if len(p_norm) >= 10:
            is_copied = p_norm in prev_norm
            if not is_copied:
                for ppn in prev_p_norms:
                    if difflib.SequenceMatcher(None, p_norm, ppn).ratio() >= 0.85:
                        is_copied = True
                        break
            if is_copied:
                removed_any = True
                continue
        kept.append(p)

    if not kept:
        return rep

    if removed_any:
        return delimiter.join(kept).strip()
    return rep


_FOOD_OR_ITEM_KEYWORDS = (
    "カステラ",
    "スイーツ",
    "お菓子",
    "おかし",
    "味",
    "食べ",
    "飲",
    "アイス",
    "ケーキ",
    "チョコ",
    "プリン",
    "パン",
    "ラーメン",
    "食品",
    "ご飯",
    "ごはん",
)


def sanitize_unrelated_media_hallucination(text: str, user_text: str = "") -> str:
    """食品・飲食物・一般商品についての質問に対し、LLMが『カセットテープやCD』『音楽作品』『アーティスト名』などと架空の音楽ジャンルへ飛躍した妄想文を除去・補正する。"""
    t = str(text or "")
    if not t or not user_text:
        return t

    # ユーザー発言が食品・飲食物・日常商品に関するものか確認
    is_food_context = any(kw in user_text for kw in _FOOD_OR_ITEM_KEYWORDS)
    # かつユーザー発言に「CD」「カセット」「曲」「音楽」「アーティスト」が含まれていない場合
    user_mentions_music = any(
        kw in user_text
        for kw in ("CD", "cd", "カセット", "曲", "音楽", "バンド", "アーティスト", "アルバム", "シングル")
    )
    if not is_food_context or user_mentions_music:
        return t

    # 音楽メディアへの不自然な飛躍・ハルシネーションをサニタイズ
    t = re.sub(r"って名前の(?:カセットテープ|CD|カセット|レコード)(?:や(?:カセットテープ|CD|カセット|レコード))?を探しても", "を探しても", t)
    t = re.sub(r"(?:カセットテープ|CD|カセット|レコード)(?:や(?:カセットテープ|CD|カセット|レコード))?を探しても", "探しても", t)
    t = re.sub(r"(?:もしかして[、,\s]*)?(?:.+?[と、,\s]+)?何か別の音楽作品が混ざっちゃった[？?]?\s*", "", t)
    t = re.sub(r"それとも[「『\"]?[^「」『\"\n]{1,20}[」』\"]?はアーティスト名とかの別物[？?]?\s*", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# 相手が使っていない顔文字や語尾への冤罪ツッコミ検出用パターン
_FALSE_STYLE_COMPOUND_PATTERN = re.compile(
    r"(?:その|使って(?:いる|た|る)|そんな)?(?:顔文字|絵文字)\s*と\s*"
    r"(?:[「『“\"〜~]*[^」』”\"\n]{1,15}[」』”\"]*(?:の|という|って)?語尾|その語尾|使って(?:いる|た|る)語尾|その口調|その話し方)"
    r"[、，\s]*"
)

_FALSE_KAOMOJI_SINGLE_PATTERN = re.compile(
    r"(?:その|使って(?:いる|た|る)|そんな)(?:顔文字|絵文字)"
    r"[、，はがのをにへとで\s]*"
)

_FALSE_QUOTE_TAIL_PATTERN = re.compile(
    r"([「『“\"〜~]+([^「」『』”\"\n]{1,15})[」』”\"〜~]*(?:の|という|って)?語尾)"
    r"[、，はがのをにへとで\s]*"
)

_HONORIFIC_POSSESSIVE_RE = re.compile(
    r"[ぁ-んァ-ヶ一-龠A-Za-z0-9]+(?:さん|くん|君|ちゃん|氏|先生)の"
)


def _is_preceded_by_third_party_subject(text: str, start_pos: int) -> bool:
    """マッチ開始位置より前の同一節・文内に『〇〇さんの』のような第三者の主語が存在するかを判定する。"""
    prev_text = text[:start_pos]
    boundary_pos = max(prev_text.rfind("。"), prev_text.rfind("！"), prev_text.rfind("？"), prev_text.rfind("\n"))
    clause = prev_text[boundary_pos + 1:] if boundary_pos != -1 else prev_text
    return bool(_HONORIFIC_POSSESSIVE_RE.search(clause))


def _user_has_kaomoji_or_emoji(text: str) -> bool:
    """ユーザー発言内に顔文字や絵文字が含まれているかを判定する。"""
    if not text:
        return False
    if extract_kaomoji_candidates(text):
        return True
    if re.search(r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]", text):
        return True
    return False


def _normalize_tail_quote_term(term: str) -> str:
    """語尾のクォート内から波ダッシュ等の装飾を除去して実体文字列を抽出する。"""
    return re.sub(r"^[「『“\"〜~\s]+|[」』”\"〜~\s]+$", "", term).strip()


def sanitize_false_style_accusation(reply: str, *, user_text: str = "") -> str:
    """最新の相手発言に顔文字が存在しないにもかかわらず、返答内で『その顔文字』等と指摘している場合、
    または相手発言に存在しない語尾を『その語尾』『『〜』の語尾』と指摘している主客不整合・冤罪ツッコミを検知しサニタイズする。
    """
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r or not u:
        return r

    user_has_kaomoji = _user_has_kaomoji_or_emoji(u)
    modified = False

    # 1. 複合冤罪パターン（顔文字 + 語尾/口調）: 相手が顔文字を使っていない場合
    if not user_has_kaomoji:
        for m in list(_FALSE_STYLE_COMPOUND_PATTERN.finditer(r)):
            if not _is_preceded_by_third_party_subject(r, m.start()):
                r = r[:m.start()] + r[m.end():]
                modified = True
                break

    # 2. 単独顔文字冤罪パターン: 相手が顔文字を使っていない場合
    if not user_has_kaomoji:
        for m in list(_FALSE_KAOMOJI_SINGLE_PATTERN.finditer(r)):
            if not _is_preceded_by_third_party_subject(r, m.start()):
                r = r[:m.start()] + r[m.end():]
                modified = True
                break

    # 3. クォート語尾冤罪パターン: 相手が言っていない語尾
    for m in list(_FALSE_QUOTE_TAIL_PATTERN.finditer(r)):
        if not _is_preceded_by_third_party_subject(r, m.start()):
            quote_term = _normalize_tail_quote_term(m.group(2))
            if quote_term and quote_term not in u:
                r = r[:m.start()] + r[m.end():]
                modified = True
                break

    if not modified:
        return reply

    # 句読点・空白の整理
    r = re.sub(r"^[、，\s]+", "", r)
    r = re.sub(r"[ \t]{2,}", " ", r).strip()

    # 助詞のみが文頭に残った場合の微調整（例: 「には少し怖くて」 -> 「少し怖くて」等）
    if re.match(r"^(?:には|にも|へは|へも)\s*", r):
        r = re.sub(r"^(?:には|にも|へは|へも)\s*", "", r)

    # 除去の結果、極端に短くなった場合（例: 5文字未満）のフォールバック
    if len(r) < 5:
        return "急に言われてもびっくりしちゃいますよね…！"

    return r


_GRANDIOSE_BOKE_PATTERN = re.compile(
    r"(?:"
    r"教え(?:る|てやる|よう|てあげる|てあげよう)"
    r"|伝授(?:する|してやる|しよう|してあげる)"
    r"|見せ(?:る|てやる|よう|てあげる)"
    r"|授け(?:る|よう)"
    r"|叩き込(?:む|んでやる)"
    r"|ご覧に入(?:れる|れよう)"
    r")"
)

_TOPIC_MENTION_FOLLOWING_PATTERN = re.compile(
    r"^(?:"
    r"とは[、，\s]*"
    r"|とは"
    r"|は[、，\s]*"
    r"|は"
    r"|って[、，\s]*"
    r"|って(?:何|誰|どう|いう|のこと|こと|ですか)?"
    r"|というの[はが]"
    r"|のこと[はが]?"
    r"|について"
    r"|ですか[？?]*"
    r"|じゃん"
    r")"
)


def strip_opening_quote_echo(reply: str, user_text: str = "") -> str:
    """相手の最新メッセージと一致する文頭のカギ括弧付きオウム返し（例: 『「神頼みをお前に教える」…！？』）を検知して除去する。"""
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r or not u:
        return r

    clean_u = re.sub(r"^[（\(].+?(?:への返信|の[「『].+?[」』]への返信)[）\)]\s*", "", u).strip()
    clean_u_norm = _normalize_compare_text(clean_u)
    if not clean_u_norm or len(clean_u_norm) < 2:
        return r

    # 冒頭の短い感嘆詞（ぴゃっ…！等）を許容して文頭引用をマッチ
    prefix_m = re.match(r"^(?:(?:[ぁ-んァ-ヶa-zA-Z]{1,8}[…!！\?？\s]+)*)", r)
    prefix_len = prefix_m.end() if prefix_m else 0
    sub_r = r[prefix_len:]

    quote_m = re.match(r"^[「『]([^」』]+)[」』]([…!！？\s\.\?]*)", sub_r)
    if quote_m:
        quote_text = quote_m.group(1).strip()
        quote_norm = _normalize_compare_text(quote_text)
        if not quote_norm:
            return r

        # 保護ガード: カギ括弧の直後または約物後に助詞（「は」「って」「とは」「とは、」等）が続き、
        # 後続が解説や定義、リアクションである場合はトピック言及・固有名詞と判定して絶対に除去しない
        after_quote = sub_r[quote_m.end(1) + 1:].lstrip()
        after_punct = sub_r[quote_m.end():].lstrip()
        if _TOPIC_MENTION_FOLLOWING_PATTERN.match(after_quote) or _TOPIC_MENTION_FOLLOWING_PATTERN.match(after_punct):
            return r

        # 文頭オウム返しとして除去するのは、以下のいずれかを満たす場合に限定する：
        # 1. quote_norm == clean_u_norm（完全一致）
        # 2. カギ括弧内の文字数が相手発言全体の80%以上を占め、かつ大仰ボケの文末助動詞（「教える」「伝授する」等）を含む場合
        is_exact_match = (quote_norm == clean_u_norm)
        is_grandiose_boke = False
        if len(clean_u_norm) > 0:
            ratio = len(quote_norm) / len(clean_u_norm)
            if ratio >= 0.8 and _GRANDIOSE_BOKE_PATTERN.search(quote_text):
                is_grandiose_boke = True

        if is_exact_match or is_grandiose_boke:
            remainder = sub_r[quote_m.end():].lstrip("、， \t\n　")
            head = r[:prefix_len]
            new_r = (head + remainder).strip()
            if new_r:
                r = new_r

    return r


_META_DEFENSIVE_PATTERN = re.compile(
    r"(?:^|[、，。！？!?\s])"
    r"(?:(?:[ぁ-んァ-ヶ一-龠A-Za-z0-9]+|私(?:自身)?|自分)は)?"
    r"\s*ただ[^、。！？\n]{2,35}?"
    r"(?:と言った|って言った|とツッコんだ|ってツッコんだ|とつっこんだ|ってつっこんだ|ツッコミた|つっこみた|返した|書いた|した)だけなのに"
    r"[、，\s]*"
)

# 「（ただ）〜ドキッとしただけ（なのに/なんだから/の話を）」系メタ弁解節
_DOKITTO_DEFENSIVE_PATTERN = re.compile(
    r"(?:^|[、，。！？!?\s…‥])"
    r"(?:(?:ただ|さっきは|その|今回は)[、，\s…‥]*)*"
    r"(?:(?:[ぁ-んァ-ヶ一-龠A-Za-z0-9]+|私(?:自身)?|自分)(?:が|は)\s*)?"
    r"(?:(?:ただ|さっきは|その|今回は)[、，\s…‥]*)*"
    r"(?:(?:ちょっと|少し|単に)\s*)?"
    r"ドキッとしただけ(?:なのに|なんだから|の話を|なんですから|なんですよ|なんです|なだけ(?:で|なのに)?|だもん|じゃん)[^。！？!?\n]*?[。！？!?…‥]*"
)

# 「被害者面」言いがかりフレーズ
_VICTIM_BLAME_PATTERN = re.compile(
    r"(?:^|[、，。！？!?\s…‥])"
    r"(?:(?:急に|何故か|なぜか|勝手に)\s*)?"
    r"(?:(?:[ぁ-んァ-ヶ一-龠A-Za-z0-9]+|あなた|あんた|お前)(?:さん|君|くん|ちゃん)?(?:が|は)\s*)?"
    r"被害者面(?:しないで(?:ください)?|して(?:問い詰めて(?:くる|こないで|きた)?|責めないで|こないで|文句言わないで)?|するの[はを]|ばっかり)"
    r"(?:から|ので|のに)?"
    r"[^。！？!?\n]*?[。！？!?…‥]*"
)


def sanitize_meta_defensiveness(reply: str) -> str:
    """『私は〜と言っただけなのに』『ただ〜ツッコミただけなのに』といった
    過去の自発言の意図をメタ説明・自己弁護する定型フレーズや、
    『〜ドキッとしただけなんだから』『被害者面しないでください』等の言いがかり・自己弁護節をサニタイズする。
    """
    r = str(reply or "").strip()
    if not r:
        return ""

    if _META_DEFENSIVE_PATTERN.search(r):
        r = _META_DEFENSIVE_PATTERN.sub("", r).strip()

    if _DOKITTO_DEFENSIVE_PATTERN.search(r):
        r = _DOKITTO_DEFENSIVE_PATTERN.sub("", r).strip()

    if _VICTIM_BLAME_PATTERN.search(r):
        r = _VICTIM_BLAME_PATTERN.sub("", r).strip()

    r = re.sub(r"^[、，\s…‥・！？!?]+", "", r).strip()
    r = re.sub(r"^(?:ただ|さっきは|から|ので|のに)[、，\s…‥]+", "", r).strip()
    r = re.sub(r"^[、，\s…‥・！？!?]+", "", r).strip()
    r = re.sub(r"[、，\s]+$", "", r).strip()
    r = re.sub(r"[、，]{2,}", "、", r).strip()

    # もしサニタイズにより全文が消失してしまった場合は、無害なリアクションに置換
    if not r:
        return "急に言われてびっくりしちゃっただけなんですよ…！"

    return r


def sanitize_third_party_reference_explanation(
    reply: str,
    *,
    target_name: str = "",
    other_names: list[str] | set[str] | None = None,
) -> str:
    """返信時に、宛先以外の第三者の名前を勝手に引き合いに出して
    『〇〇さんが〜と言ってるのとは別の話』『〇〇さんの言う通り』と解説・比較している文節をサニタイズする。
    """
    r = str(reply or "").strip()
    if not r:
        return ""

    eff_others: set[str] = set()
    if other_names:
        for name in other_names:
            s_name = str(name).strip()
            if s_name and s_name != target_name and len(s_name) >= 2:
                eff_others.add(s_name)

    if not eff_others:
        return r

    for name in eff_others:
        escaped = re.escape(name)
        # 1. 比較・別話構文: 『〇〇が〜と言ってるのとは、また別の話』 -> 『それはまた別の話』
        pat_diff = re.compile(
            rf"[「『]?{escaped}[」』]?(?:さん|ちゃん|くん|君)?が[^。！？!?\n]{{0,60}}?"
            rf"(?:言っ(?:てる|た|ている)|言う|言った|みたいに|のように)(?:の)?とは[、，\s]*(?:また)?別の",
            re.IGNORECASE,
        )
        if pat_diff.search(r):
            r = pat_diff.sub("それはまた別の", r).strip()

        # 2. 同調構文: 『〇〇さんの言う通り、』 -> 『』
        pat_agree = re.compile(
            rf"[「『]?{escaped}[」』]?(?:さん|ちゃん|くん|君)?の"
            rf"(?:言う通り|言うとおり|言った通り|言ったとおり|言うように|言ってるように)[、，\s]*",
            re.IGNORECASE,
        )
        if pat_agree.search(r):
            r = pat_agree.sub("", r).strip()

    r = re.sub(r"^[、，\s…‥・！？!?]+", "", r).strip()
    r = re.sub(r"[、，]{2,}", "、", r).strip()
    return r


def sanitize_harsh_daro_endings(reply: str) -> str:
    """福丸小糸らしからぬ粗暴な男勝り語尾（『〜高すぎだろって』『〜だろ！』等）を
    『〜高すぎるんじゃないかって』『〜ですよね！』等に軟化補正する。
    """
    r = str(reply or "")
    if not r:
        return ""

    # 1. 〜すぎだろって -> 〜すぎるんじゃないかって
    r = re.sub(
        r"([^\s、，。！？!?\n]+?すぎ)だろって",
        r"\1るんじゃないかって",
        r,
    )

    # 2. 〜だろって -> 〜なんじゃないかって
    r = re.sub(
        r"([^\s、，。！？!?\n]+?)だろって",
        r"\1なんじゃないかって",
        r,
    )

    # 3. 〜すぎだろ (文末・約物・改行前) -> 〜すぎるんじゃないでしょうか / 〜すぎるんじゃないですか
    def _repl_sugidaro(m: re.Match) -> str:
        stem = m.group(1)
        punct = m.group(2)
        if any(ex in punct for ex in ("！", "!")):
            return f"{stem}るんじゃないですか{punct}"
        return f"{stem}るんじゃないでしょうか{punct}"

    r = re.sub(
        r"([^\s、，。！？!?\n]+?すぎ)だろ([！!？?…‥。\n\s]|$)",
        _repl_sugidaro,
        r,
    )

    # 4. 粗暴な 〜だろ (文末・約物・改行前)
    # ※ 『くだろ』『たどろ』等の動詞語幹や『だろう』を除く
    def _repl_daro(m: re.Match) -> str:
        prev = m.group(1)
        punct = m.group(2)
        if prev in ("く", "た", "辿", "下"):
            return m.group(0)
        if any(q in punct for q in ("？", "?")):
            return f"{prev}ですよね{punct}"
        if any(ex in punct for ex in ("！", "!")):
            return f"{prev}ですよ{punct}"
        return f"{prev}ですよね{punct}"

    r = re.sub(
        r"([^くた辿下\s、，。！？!?\n])だろ([！!？?…‥。\n\s]|$)",
        _repl_daro,
        r,
    )

    return r


_AI_META_CONCEPT_RE = re.compile(
    r"(?:製造者|開発者|製作者|制作者|開発元|製造元)(?:という概念|なんてもの|というもの)?(?:が|は|も)?",
    re.IGNORECASE,
)

_AI_META_LLM_TERMS_RE = re.compile(
    r"(?:大規模言語モデル|LLM|言語モデル)",
    re.IGNORECASE,
)

_AI_META_SELF_RE = re.compile(
    r"(?:(?:私|わたし|あたし|自分|ボク|ぼく|うち|俺|おれ)(?:は|って)?\s*)?"
    r"(?:AI|人工知能|プログラム|ボット|bot|大規模言語モデル|LLM)"
    r"(?:(?:な(?:の(?:で|ですから?|だが?))?)|ですから?|だから?|ですし|として|なんですから?)"
    r"[、，\s]*",
    re.IGNORECASE,
)

_AI_META_ASSERTION_RE = re.compile(
    r"(?:(?:私|わたし|あたし|自分|ボク|ぼく|うち|俺|おれ)(?:は|って)?\s*)?"
    r"(?:AI|人工知能|プログラム|ボット|bot)"
    r"(?:です|だ|なんですよ?|なんですから?)"
    r"([、，。！？!?\s]|$)",
    re.IGNORECASE,
)


def sanitize_ai_meta_statements(reply: str) -> str:
    """『私はAIなので、製造者という概念が…』『私は人工知能ですから』といった
    AI・人工知能・プログラム自認や製造者等のメタ発言を検知し、
    ペルソナとしての表現（『そんなこと言われても』『よくわからないですけど』等）に置換・除去する。
    """
    r = str(reply or "").strip()
    if not r:
        return ""

    has_meta = bool(
        re.search(
            r"(?:AI|人工知能|プログラム|ボット|bot|大規模言語モデル|LLM|製造者|開発者|製作者|制作者)",
            r,
            re.IGNORECASE,
        )
    )
    if not has_meta:
        return r

    try:
        from lib.config_utils import cfg
        fp = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    except Exception:
        fp = ""

    # 1. 製造者・開発者という概念等の置換
    def _replace_concept(m: re.Match[str]) -> str:
        matched = m.group(0)
        if matched.endswith(("が", "は", "も")):
            return "そういう難しいことは"
        return "そういう難しいこと"

    r = _AI_META_CONCEPT_RE.sub(_replace_concept, r)

    # 2. 大規模言語モデル・LLM用語の置換
    r = _AI_META_LLM_TERMS_RE.sub("そういう難しいこと", r)

    # 3. AI自認節（〜AIなので、〜人工知能ですから）の置換
    self_re = _AI_META_SELF_RE
    assertion_re = _AI_META_ASSERTION_RE
    if fp and fp not in ("私", "わたし", "あたし", "自分", "ボク", "ぼく", "うち", "俺", "おれ"):
        fp_esc = re.escape(fp)
        self_re = re.compile(
            rf"(?:(?:{fp_esc}|私|わたし|あたし|自分|ボク|ぼく|うち|俺|おれ)(?:は|って)?\s*)?"
            r"(?:AI|人工知能|プログラム|ボット|bot|大規模言語モデル|LLM)"
            r"(?:(?:な(?:の(?:で|ですから?|だが?))?)|ですから?|だから?|ですし|として|なんですから?)"
            r"[、，\s]*",
            re.IGNORECASE,
        )
        assertion_re = re.compile(
            rf"(?:(?:{fp_esc}|私|わたし|あたし|自分|ボク|ぼく|うち|俺|おれ)(?:は|って)?\s*)?"
            r"(?:AI|人工知能|プログラム|ボット|bot)"
            r"(?:です|だ|なんですよ?|なんですから?)"
            r"([、，。！？!?\s]|$)",
            re.IGNORECASE,
        )

    def _replace_self(m: re.Match[str]) -> str:
        full = m.group(0)
        if fp and fp in full:
            return f"{fp}はそんなこと言われても、"
        elif any(p in full for p in ("私", "わたし", "あたし", "自分", "ボク", "ぼく", "うち", "俺", "おれ")):
            return "そんなこと言われても、"
        return "よくわからないですけど、"

    r = self_re.sub(_replace_self, r)

    # 4. 「AIです」「人工知能です」等の断定文の置換
    def _replace_assertion(m: re.Match[str]) -> str:
        full = m.group(0)
        punct = m.group(1) or ""
        if fp and fp in full:
            return f"{fp}にはよくわからないです{punct}"
        return f"よくわからないです{punct}"

    r = assertion_re.sub(_replace_assertion, r)

    # 5. 整形
    r = re.sub(r"^[、，\s…‥・！？!?]+", "", r).strip()
    r = re.sub(r"[、，\s]+$", "", r).strip()
    r = re.sub(r"[、，]{2,}", "、", r).strip()
    if fp:
        r = re.sub(rf"(?:{re.escape(fp)}はそんなこと言われても、\s*)+(?={re.escape(fp)}はそんなこと言われても、)", "", r).strip()
    r = re.sub(r"(?:そんなこと言われても、\s*)+(?=そんなこと言われても、)", "", r).strip()
    r = re.sub(r"(?:よくわからないですけど、\s*)+(?=よくわからないですけど、)", "", r).strip()

    if not r:
        return "そんなこと言われても、よくわからないです…！"

    return r


_ELLIPSIS_BANG_RE = re.compile(r"(?:…+|\.{2,}|‥+)[！!]+")


def sanitize_excessive_ellipsis_bang(text: str, *, max_count: int = 1) -> str:
    """同一テキスト内で過剰に連発される『…！』を指定回数（デフォルト1回）までに抑制し、超過分を『。』に置換する。"""
    raw = str(text or "")
    if not raw or max_count < 0:
        return raw

    seen = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        if seen <= max_count:
            return match.group(0)
        return "。"

    result = _ELLIPSIS_BANG_RE.sub(_replace, raw)
    result = re.sub(r"。{2,}", "。", result)
    return result


_SMUG_BLUFF_PATTERN = re.compile(
    r"(^|[、，。！？!?\s…‥])"
    r"(?:(?:あの|その)\s*)?(?:ドヤ|どや)顔で\s*"
    r"(?:言われ|来られ|見られ|迫られ)(?:たら|ても)"
    r"[、，\s…‥]*",
)


def sanitize_ungrounded_smug_bluff(
    reply: str,
    *,
    user_text: str = "",
    context_text: str = "",
) -> str:
    """ユーザー発言および文脈に『ドヤ』『どや』が含まれていないにもかかわらず、
    LLMがハルシネーションで出力した『あのドヤ顔で言われたら〜』等の知ったかぶり情景捏造フレーズをサニタイズする。
    """
    r = str(reply or "").strip()
    if not r:
        return ""

    combined_context = f"{user_text or ''} {context_text or ''}"
    if "ドヤ" in combined_context or "どや" in combined_context:
        return r

    if not _SMUG_BLUFF_PATTERN.search(r):
        return r

    def _repl(m: re.Match[str]) -> str:
        prefix = m.group(1) or ""
        if prefix and prefix in "。！？!?…‥\n":
            return prefix
        return ""

    cleaned = _SMUG_BLUFF_PATTERN.sub(_repl, r)
    cleaned = re.sub(r"([。！？!?])+[、，]+", r"\1", cleaned)
    cleaned = re.sub(r"^[、，\s]+", "", cleaned)
    return cleaned.strip()


_CHINESE_WORD_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 「欲張り劲儿」「頑張り劲儿」等の「〜劲儿（ぶり・っぷり・気合い）」
    (re.compile(r"(?<=[ぁ-んァ-ヶ一-龠])劲儿"), "っぷり"),
    (re.compile(r"劲儿"), "気合い"),
    (re.compile(r"一点儿"), "少し"),
    (re.compile(r"一会儿"), "しばらく"),
    (re.compile(r"为什么"), "どうして"),
    (re.compile(r"怎么样"), "どう"),
    (re.compile(r"怎么"), "どう"),
    (re.compile(r"已经"), "すでに"),
    (re.compile(r"觉得"), "思う"),
    (re.compile(r"没有"), "ない"),
)

# 単独で紛れ込んだ簡体字の日本語漢字へのフォールバック置換マップ
_SIMPLIFIED_TO_JAPANESE_CHAR_MAP = str.maketrans({
    "劲": "気",
    "儿": "",
    "这": "この",
    "那": "その",
    "们": "たち",
    "吗": "か",
    "呢": "ね",
    "吧": "ね",
    "没": "無",
    "对": "対",
    "说": "説",
    "么": "",
    "过": "過",
    "还": "まだ",
    "让": "譲",
    "给": "給",
    "欢": "歓",
    "买": "買",
    "卖": "売",
    "车": "車",
    "东": "東",
    "马": "馬",
    "鸟": "鳥",
    "鸡": "鶏",
    "鱼": "魚",
    "门": "門",
    "间": "間",
    "问": "問",
    "闻": "聞",
    "关": "関",
    "开": "開",
    "电": "電",
    "飞": "飛",
    "风": "風",
    "见": "見",
    "观": "観",
    "视": "視",
    "话": "話",
    "语": "語",
    "读": "読",
    "书": "書",
    "认": "認",
    "识": "識",
    "记": "記",
    "设": "設",
    "议": "議",
    "论": "論",
    "讲": "講",
    "爱": "愛",
    "亲": "親",
    "觉": "覚",
    "惊": "驚",
    "叹": "嘆",
    "乐": "楽",
    "忧": "憂",
    "恼": "悩",
    "师": "師",
    "孙": "孫",
    "爷": "爺",
    "奶": "ミルク",
    "妈": "母",
    "爸": "父",
    "业": "業",
    "产": "産",
    "务": "務",
    "动": "動",
    "劳": "労",
    "办": "弁",
    "庆": "慶",
    "应": "応",
    "庙": "廟",
    "废": "廃",
    "亿": "億",
    "两": "両",
    "够": "十分",
    "现": "現",
    "边": "辺",
    "远": "遠",
    "进": "進",
    "运": "運",
    "连": "連",
    "迟": "遅",
    "适": "適",
    "选": "選",
    "际": "際",
    "阳": "陽",
    "阴": "陰",
    "队": "隊",
    "阶": "階",
    "陆": "陸",
    "陈": "陳",
    "险": "険",
    "实": "実",
    "头": "頭",
    "长": "長",
})


def sanitize_chinese_leak(reply: str) -> str:
    """返答文に混入した中国語語彙（『劲儿』等）や簡体字を自然な日本語表現へ置換・除去する（英単語はそのまま保持）。"""
    r = str(reply or "").strip()
    if not r:
        return ""
    for pat, repl in _CHINESE_WORD_REPLACEMENTS:
        if pat.search(r):
            r = pat.sub(repl, r)
    # 残存する簡体字専用文字があれば変換
    from .validation import _SIMPLIFIED_CHINESE_CHARS_RE
    if _SIMPLIFIED_CHINESE_CHARS_RE.search(r):
        r = r.translate(_SIMPLIFIED_TO_JAPANESE_CHAR_MAP)
    return r.strip()







