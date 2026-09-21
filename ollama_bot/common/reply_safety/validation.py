"""生成文の品質・安全性検証（オウム返し、文末途切れ、非日本語、異常返答、使用不能返答等の判定）。"""
from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING

from .cleaner import (
    _normalize_compare_text,
    _normalize_topic_echo_text,
    _opening_echo_fragment,
    strip_leading_interjections,
    strip_markdown_artifacts,
)
from .patterns import (
    looks_like_multi_turn_output,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
)

if TYPE_CHECKING:
    from .sanitizer import sanitize_generated_reply


def _is_validation_ending_clause(clause: str) -> bool:
    c = str(clause or "").strip()
    return bool(
        re.search(
            r"(?:"
            r"[のん]ですね|[のん]ですか|ですね|ですか|だね|だよね|ますね|ましたね|"
            r"てしまった[のん]?|ちゃった[のん]?|[しされ]たんですね|[しされ]たんですか|"
            r"[たて]んです[かね]|てきたんです[かね]|てたんです[かね]|ていたんです[かね]|"
            r"[たて]んですね|てきたんですね|てたんですね|ていたんですね|"
            r"たのですね|たのですか|てきたのですね|てきたのですか"
            r")",
            c,
        )
    )


def _is_short_noun_reaction_clause(clause: str) -> bool:
    """短い名詞・ボケ・単語提示に対する感嘆・ツッコミ・受容語尾（〜か…！、〜とは…！等）を判定する。"""
    c = str(clause or "").strip()
    return bool(
        re.search(
            r"(?:"
            r"か…！|か！|かぁ|かよ|か…|とは…！|とは！|って…！|ってこと[かね]|ですか…！|ですね…！|"
            r"(?:か|とは|って)$"
            r")",
            c,
        )
    )


def _is_compliance_or_acceptance_clause(clause: str) -> bool:
    """相手の注意・依頼・約束・ルール指摘に対して、AIが受容・承諾・遵守する返答であるかを判定する。"""
    c = str(clause or "").strip()
    return bool(
        re.search(
            r"(?:"
            r"約束[、を]*[しっか]*り?守[るりっ]|"
            r"[気配]をつけ[るますて]|"
            r"言われた通りに[するし]|"
            r"安心してください|"
            r"使わない[よう]*にする|"
            r"もう[しませ]*[んない]|"
            r"ごめんなさい|すみません|"
            r"了解[ですした]*|承知[いたしま]*[すした]"
            r")",
            c,
        )
    )


def _looks_like_opening_topic_echo(user_text: str, assistant_text: str) -> bool:
    user = _normalize_topic_echo_text(user_text)
    head = _opening_echo_fragment(assistant_text)
    if len(head) < 2 or len(head) > 16 or not user:
        return False

    # アシスタント返答が十分に長く（20文字以上）、自然な受容・共感文（〜なんですね等）や
    # 承諾・遵守文（約束守るよ、気をつけるね等）で後半に独立した展開がある場合は過剰検知を防止する
    raw_text = str(assistant_text or "").strip()
    first_line = raw_text.splitlines()[0] if raw_text else ""
    effective_line = strip_leading_interjections(first_line) or first_line
    first_sentence = re.split(r"[。!！?？]", effective_line, maxsplit=1)[0].strip()
    first_clause = re.split(r"[、。,.!！?？…]", effective_line, maxsplit=1)[0].strip()
    raw_first_sentence = re.split(r"[。!！?？]", first_line, maxsplit=1)[0].strip()
    raw_first_clause = re.split(r"[、。,.!！?？…]", first_line, maxsplit=1)[0].strip()
    is_validation = (
        _is_validation_ending_clause(first_sentence)
        or _is_validation_ending_clause(first_clause)
        or _is_validation_ending_clause(raw_first_sentence)
        or _is_validation_ending_clause(raw_first_clause)
    )
    is_acceptance = (
        _is_compliance_or_acceptance_clause(first_sentence)
        or _is_compliance_or_acceptance_clause(first_clause)
        or _is_compliance_or_acceptance_clause(raw_first_sentence)
        or _is_compliance_or_acceptance_clause(raw_first_clause)
    )
    is_noun_reaction = (
        len(user) <= 20
        and (
            _is_short_noun_reaction_clause(first_sentence)
            or _is_short_noun_reaction_clause(first_clause)
            or _is_short_noun_reaction_clause(raw_first_sentence)
            or _is_short_noun_reaction_clause(raw_first_clause)
        )
    )
    if (is_validation or is_acceptance or is_noun_reaction) and len(raw_text) >= 20:
        return False

    if head in user:
        return True
    if len(head) < 4:
        return False

    longest = difflib.SequenceMatcher(None, head, user).find_longest_match(0, len(head), 0, len(user))
    return longest.a == 0 and longest.size >= max(4, int(len(head) * 0.50))


def looks_like_parrot_reply(user_text: str, assistant_text: str) -> bool:
    u = _normalize_compare_text(user_text)
    a = _normalize_compare_text(assistant_text)

    if not u or not a:
        return False

    if a == u:
        return True

    if len(u) < 5:
        return False

    matcher = difflib.SequenceMatcher(None, u, a)
    overall_ratio = matcher.ratio()
    # 全体丸写し・高類似度（語尾だけ変えたような復唱）は、受容句や承諾句があってもオウム返し判定
    if len(u) >= 15 and overall_ratio >= 0.85:
        return True

    # アシスタント返答が十分に長く（20文字以上）、自然な受容・共感文（〜なんですね等）や
    # 承諾・遵守文（約束守るよ、気をつけるね等）で後半に独立した展開がある場合は過剰検知を防止する
    raw_text = str(assistant_text or "").strip()
    first_line = raw_text.splitlines()[0] if raw_text else ""
    effective_line = strip_leading_interjections(first_line) or first_line
    first_sentence = re.split(r"[。!！?？]", effective_line, maxsplit=1)[0].strip()
    first_clause = re.split(r"[、。,.!！?？…]", effective_line, maxsplit=1)[0].strip()
    raw_first_sentence = re.split(r"[。!！?？]", first_line, maxsplit=1)[0].strip()
    raw_first_clause = re.split(r"[、。,.!！?？…]", first_line, maxsplit=1)[0].strip()
    is_validation = (
        _is_validation_ending_clause(first_sentence)
        or _is_validation_ending_clause(first_clause)
        or _is_validation_ending_clause(raw_first_sentence)
        or _is_validation_ending_clause(raw_first_clause)
    )
    is_acceptance = (
        _is_compliance_or_acceptance_clause(first_sentence)
        or _is_compliance_or_acceptance_clause(first_clause)
        or _is_compliance_or_acceptance_clause(raw_first_sentence)
        or _is_compliance_or_acceptance_clause(raw_first_clause)
    )
    is_noun_reaction = (
        len(u) <= 20
        and (
            _is_short_noun_reaction_clause(first_sentence)
            or _is_short_noun_reaction_clause(first_clause)
            or _is_short_noun_reaction_clause(raw_first_sentence)
            or _is_short_noun_reaction_clause(raw_first_clause)
        )
    )
    if (is_validation or is_acceptance or is_noun_reaction) and len(raw_text) >= 20:
        return False

    if _looks_like_opening_topic_echo(user_text, assistant_text):
        return True

    # ユーザー発言が短い単語・名詞（20文字以下）の場合、AI返答が十分に長く（20文字以上）
    # かつ先頭エコーでないなら、文中に話題の単語が含まれていてもオウム返しと判定しない
    if len(u) <= 20 and len(a) >= 20 and not _looks_like_opening_topic_echo(user_text, assistant_text):
        return False

    if u in a:
        return True

    matcher = difflib.SequenceMatcher(None, u, a)
    longest_match = matcher.find_longest_match(0, len(u), 0, len(a))
    longest_ratio = longest_match.size / len(u)
    if longest_ratio >= 0.50:
        return True

    # 助詞・助動詞（2文字）の偶然の一致による誤判定を防ぐため block.size >= 3 で判定
    reused_chars = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 3)
    reused_ratio = reused_chars / len(u)
    if len(u) >= 8 and reused_ratio >= 0.55:
        return True

    return False


def looks_like_truncated_reply(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False

    # 1. 括弧が開いたまま閉じられていない（未閉じ括弧）のチェック
    bracket_pairs = [
        ("「", "」"),
        ("『", "』"),
        ("（", "）"),
        ("(", ")"),
        ("【", "】"),
        ("[", "]"),
        ("“", "”"),
    ]
    for open_b, close_b in bracket_pairs:
        if t.count(open_b) > t.count(close_b):
            return True

    # 引用符（"）が奇数個（未閉じ）の場合も途切れと判定
    if t.count('"') % 2 != 0:
        return True

    valid_endings = (
        "。", "！", "？", "…", "ー", "〜", "~", "」", "』", "）", ")", "]",
        "w", "W", "♡", "♪", "★", "☆", "✨", "💢", "💦", "❕", "❓",
        "笑", "爆", "😊", "😂", "🥺", "😭", "😤", "👍"
    )
    if t.endswith(valid_endings):
        return False
        
    if len(t) > 0:
        last_char = t[-1]
        if "\U0001f000" <= last_char <= "\U0001faff" or "\u2600" <= last_char <= "\u27bf":
            return False

    hanging_suffixes = (
        "は", "が", "を", "に", "へ", "と", "より", "から", "で",
        "や", "の", "けれど", "けど", "し", "たり", "だり",
        "て", "ながら", "つつ", "ば", "たら", "なら",
        "てい", "てお", "てあ", "てみ", "てしま",
        "という", "といった", "正しい", "思って", "だから", "しかし", "ただ", "そして",
        "、", ","
    )
    if t.endswith(hanging_suffixes):
        return True

    if len(t) >= 30:
        return True

    return False


def looks_like_non_japanese_reply(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False

    scrubbed = re.sub(r"https?://\S+|www\.\S+", "", t)
    scrubbed = re.sub(r"`[^`]*`|```.*?```", "", scrubbed, flags=re.DOTALL)
    scrubbed = re.sub(r"<@!?\d+>|<#[0-9]+>|<:[^:]+:\d+>", "", scrubbed)
    japanese_chars = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", scrubbed))
    ascii_letters = len(re.findall(r"[A-Za-z]", scrubbed))

    # 日本語が一切なく、英字が6文字以上ある場合は確実に非日本語
    if japanese_chars == 0 and ascii_letters >= 6:
        return True

    # 日本語が極端に少なく（1〜2文字）、英字が圧倒的（10文字以上で全体の60%以上）な場合（例: I'll adjust to include "コ）
    total = ascii_letters + japanese_chars
    if total > 0 and ascii_letters >= 10 and ascii_letters / total >= 0.60 and ascii_letters >= japanese_chars * 3:
        return True

    return False


def looks_like_unusable_assistant_reply(text: str) -> bool:
    from .sanitizer import sanitize_generated_reply

    t = strip_markdown_artifacts(sanitize_generated_reply(text))
    compact = re.sub(r"\s+", "", t).strip()
    if not compact:
        return True
    if compact in {"…", "……", "...", "。。。", "。。", "ー", "act"}:
        return True
    # カンマ区切りの英数字トークン列（スタイルタグ漏れ）を検出する。
    # 先頭カンマあり・なし両方に対応し、「スタイル指示らしいキーワード」を含む場合はすべて unusable とする。
    # 例: ", validate_first, no_echo"  "direct, short"  "casual_japanese"
    _STYLE_TAG_MARKERS = (
        "short",
        "minimal",
        "reaction",
        "clarify",
        "answer",
        "validate",
        "direct",
        "tease",
        "no_echo",
        "noecho",
        "casual",
        "japanese",
        "echo",
        "style",
        "action",
    )
    if re.fullmatch(
        r"[,，]*(?:[A-Za-z][A-Za-z0-9_-]*)(?:[,，]+[A-Za-z][A-Za-z0-9_-]*){0,8}[,，]*＊?",
        compact,
    ):
        lowered = compact.lower().replace("-", "_")
        if any(marker in lowered for marker in _STYLE_TAG_MARKERS):
            return True
    if len(compact) <= 24 and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", compact):
        return True
    # 引用符の残骸や英文の混ざったゴミ（例: ぴゃっ", "〜なんですよ…！", avoids "わね…！" 等）を検出
    if re.search(r'["”」』][\s,，/／]+["“「『]', t) or re.search(r'["”」』][\s,，/／]+(?:avoids?|uses?|and|or|not)\b', t, flags=re.IGNORECASE):
        return True
    if re.search(r'\b(?:avoids?|uses?)\s+["“「『]', t, flags=re.IGNORECASE):
        return True
    return False


def looks_like_abnormal_assistant_reply(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    if (
        looks_like_prompt_leak(t)
        or looks_like_reasoning_leak(t)
        or looks_like_multi_turn_output(t)
        or looks_like_non_japanese_reply(t)
        or looks_like_unusable_assistant_reply(t)
    ):
        return True
    bad_markers = (
        "判定結果を生成できませんでした",
        "ファクトチェック中にエラーが発生しました",
        "Traceback (most recent call last)",
    )
    return any(marker in t for marker in bad_markers)


def _extract_final_clause_core(text: str) -> str:
    """文末の結び・オチ節を抽出し、主語プレフィックスや語尾の丁寧語・助動詞を除去した内容語コアを返す。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    last_line = lines[-1] if lines else raw
    clauses = [c.strip() for c in re.split(r"[、。,.!！?？…\n]+", last_line) if c.strip()]
    if not clauses:
        clause = last_line
    elif len(clauses[-1]) < 6 and len(clauses) >= 2:
        clause = clauses[-2] + clauses[-1]
    else:
        clause = clauses[-1]

    clause_n = _normalize_compare_text(clause)
    from lib.config_utils import cfg
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    if first_person:
        clause_n = re.sub(rf"^(?:{re.escape(first_person)}|私|あたし|自分|ぼく|僕)[はもの]", "", clause_n)
    else:
        clause_n = re.sub(r"^(?:私|あたし|自分|ぼく|僕)[はもの]", "", clause_n)
    pattern = (
        r"(?:なんですよね|なんですよ|んですよね|んですよ|なんです|ですよね|"
        r"ました|でした|だった|かった|ですよ|ですね|ますよ|ますね|です|ます|"
        r"だよ|だね|なのだ|のだ|のよ|わよ|わね|ぜ|ぞ|さ|ね|よ|な|か)+$"
    )
    return re.sub(pattern, "", clause_n)


def looks_like_similar_to_previous_reply(previous_reply: str | None, new_reply: str | None) -> bool:
    """前回返答と今回の返答が過度に重複・金太郎飴構文になっていないかを判定する。"""
    prev_n = _normalize_compare_text(previous_reply or "")
    new_n = _normalize_compare_text(new_reply or "")
    if not prev_n or not new_n:
        return False
    if prev_n == new_n:
        return True
    if len(prev_n) >= 12 and (new_n in prev_n or prev_n in new_n):
        return True

    # 全体類似度が著しく高い（80%以上の一致）
    if len(prev_n) >= 20 and len(new_n) >= 20:
        if difflib.SequenceMatcher(None, prev_n, new_n).ratio() >= 0.80:
            return True

    # 結びの文・オチ節の金太郎飴検知（ある程度長さのある返答同士で比較）
    if len(prev_n) >= 15 and len(new_n) >= 15:
        c1 = _extract_final_clause_core(previous_reply or "")
        c2 = _extract_final_clause_core(new_reply or "")
        if len(c1) >= 6 and len(c2) >= 6:
            matcher = difflib.SequenceMatcher(None, c1, c2)
            ratio = matcher.ratio()
            longest = matcher.find_longest_match(0, len(c1), 0, len(c2))
            if ratio >= 0.70 and longest.size >= 4:
                return True

    return False


_looks_too_similar_to_previous_reply = looks_like_similar_to_previous_reply


def looks_like_time_of_day_contradiction(
    reply: str,
    *,
    current_hour: int | None = None,
    user_text: str = "",
) -> bool:
    """
    返答が現在の時間帯（JST時）と著しく矛盾する状況描写や挨拶を含んでいるかを判定する。
    例: 夜・深夜（18時〜翌朝4時）に、ユーザーが朝について言及していないのに「朝から」「おはよう」「今朝」などと決めつける場合。
    """
    if not reply:
        return False
    if current_hour is None:
        from lib.date_utils import now_jst
        current_hour = now_jst().hour

    reply_raw = str(reply).strip()
    user_raw = str(user_text).strip()

    # 夜・深夜（18時〜翌朝4時）の判定
    if current_hour >= 18 or current_hour <= 4:
        morning_user_markers = ("朝", "あさ", "morning", "早朝", "今朝")
        user_mentions_morning = any(m in user_raw for m in morning_user_markers)
        if not user_mentions_morning:
            morning_reply_markers = (
                "朝から", "朝っぱら", "あさから", "おはよう", "今朝は", "朝ご飯", "朝ごはん", "朝飯",
                "朝早く", "朝の光", "朝一番",
            )
            if any(marker in reply_raw for marker in morning_reply_markers):
                return True

    # 朝・日中（6時〜16時）の判定
    elif 6 <= current_hour <= 16:
        evening_user_markers = ("夜", "よる", "晩", "ばん", "night", "今夜", "夕方")
        user_mentions_night = any(m in user_raw for m in evening_user_markers)
        if not user_mentions_night:
            night_reply_markers = (
                "こんばんは", "今夜は", "夜遅く", "おやすみ", "夜分",
            )
            if any(marker in reply_raw for marker in night_reply_markers):
                return True

    return False


def looks_like_character_deviation(
    reply: str,
    *,
    user_text: str = "",
) -> bool:
    """
    返答がキャラクター設定から著しく逸脱し、
    相手に対する暴言・罵倒、乱暴な二人称（あんた、お前等）、または攻撃的・威嚇的な口調になっていないかを判定する。
    """
    r = str(reply or "").strip()
    if not r:
        return False

    u = str(user_text or "").strip()

    # 1. 禁止された乱暴な二人称（ユーザー発言からの引用でない場合）
    pronoun_pattern = r"(?:^|[\s、。！？!?.…「」『』\n])(あんた|お前|おまえ|貴様|てめえ|手前)(?:[はがのもにへよりからでっての]|[、。！？!?.…\s\n]|$)"
    match = re.search(pronoun_pattern, r)
    if match:
        word = match.group(1)
        if word not in u:
            return True

    # 2. 禁止された直接の罵倒語・攻撃的命令語
    abusive_pattern = (
        r"(?:"
        r"黙れ|"
        r"黙ってろ|"
        r"すっこんでろ|"
        r"消えろ|"
        r"死ね|"
        r"くたばれ|"
        r"うぜえ|"
        r"キモいんだよ|"
        r"アホンダラ"
        r")"
    )
    match_abuse = re.search(abusive_pattern, r)
    if match_abuse:
        word = match_abuse.group(0)
        # ユーザー発言に元々含まれていない罵倒語なら確実にキャラクター逸脱
        if word not in u:
            return True

    # 3. 相手に対する威嚇・攻撃的語尾（〜てろよ、〜しろよ、〜じゃねえよ等）
    aggressive_tail_pattern = r"(?:てろよ|しろよ|するなよ|すんなよ|じゃねえよ|だろよ)[!！…\.\?？~〜\s]*$"
    if re.search(aggressive_tail_pattern, r):
        from lib.config_utils import cfg
        polite_pattern = str(cfg("CHARACTER_DEVIATION_POLITE_PATTERNS", "") or "").strip()
        if not polite_pattern:
            polite_pattern = r"(?:です|ます|でした|ました)"
        has_polite_or_fear = bool(re.search(polite_pattern, r))
        if not has_polite_or_fear:
            return True

    return False
