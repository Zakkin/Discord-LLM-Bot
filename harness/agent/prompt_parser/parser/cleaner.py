"""テキストの正規化、不要記号・Markdown装飾除去、一人称や感謝表現の補正処理。"""
from __future__ import annotations

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
        if target_san:
            return f"{sep}{target_san}{tail}"
        return tail

    def _repl_start(match: re.Match) -> str:
        tail = match.group(2) or ""
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



