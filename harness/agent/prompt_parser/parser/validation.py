"""生成文の品質・安全性検証（オウム返し、文末途切れ、非日本語、異常返答、使用不能返答等の判定）。"""
from __future__ import annotations

import difflib
import logging
import re
from typing import TYPE_CHECKING, Optional, Sequence

log = logging.getLogger("harness.agent.prompt_parser.parser.validation")

from .cleaner import (
    _normalize_compare_text,
    _normalize_topic_echo_text,
    _opening_echo_fragment,
    extract_effective_first_clause,
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
            r"たのですね|たのですか|てきたのですね|てきたのですか|"
            r"[のん]?ですけど|[のん]?なんですけど|[のん]?ですが|[のん]?なんですが|"
            r"[のん]?だけど|[のん]?なんだけど|[のん]?だが|[のん]?なんだが"
            r")",
            c,
        )
    )


def _is_short_noun_reaction_clause(clause: str) -> bool:
    """短い名詞・ボケ・単語提示に対する感嘆・ツッコミ・受容語尾（〜か…！、〜とは…！、〜！、〜…！等）を判定する。"""
    c = str(clause or "").strip()
    return bool(
        re.search(
            r"(?:"
            r"か…！|か！|かぁ|かよ|か…|とは…！|とは！|って…！|ってこと[かね]|ですか…！|ですね…！|"
            r"[!！…]+[0-9A-Za-zぁ-んァ-ヶ一-龥]*|"
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


def _is_greeting_or_introduction_clause(clause: str) -> bool:
    """相手の挨拶・自己紹介・友好的な声かけ（初めまして、よろしく、仲良く等）を判定する。"""
    c = str(clause or "").strip()
    return bool(
        re.search(
            r"(?:"
            r"初めまして|はじめまして|"
            r"こんにちは|こんばん[はわ]|おはよう[ございま]*[すした]*|"
            r"よろしく[お願ねが]*[い致しま]*[すした]*|"
            r"仲良[くし]*[ましょ]*[うね]*|"
            r"いらっしゃい|ようこそ|"
            r"(?:どうぞ|今後とも)[、\s]*よろしく"
            r")",
            c,
        )
    )



def _match_rhetorical_question_tsukkomi(text: str) -> re.Match[str] | None:
    """文頭（感嘆詞除去後）が反問・ツッコミ形式（『〇〇？！』『〇〇…！？』『〇〇?!』等）であるかを判定しマッチを返す。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    first_line = raw.splitlines()[0] if raw else ""
    line = strip_leading_interjections(first_line) or first_line
    # 冒頭1〜20文字の名詞・語句に続き、疑問符と感嘆符の双方が組み合わさった反問ツッコミ記号（？！、！？、?!、!?等）を検出
    return re.match(
        r"^([^、。,.!！?？…\n]{1,20})(?:…|\.{2,})*(?:[?？]+[!！]+|[!！]+[?？]+)",
        line,
    )


def _is_rhetorical_question_tsukkomi_reply(
    user_text: str,
    assistant_text: str,
    overall_ratio: float,
) -> bool:
    """文頭で相手の言葉を反問・ツッコミ（『〇〇？！』『〇〇…！？』等）して後続で独自の展開を述べる返答であるかを判定する。"""
    raw_text = str(assistant_text or "").strip()
    if len(raw_text) < 15 or overall_ratio >= 0.50:
        return False

    match = _match_rhetorical_question_tsukkomi(raw_text)
    if not match:
        return False

    # 文頭のツッコミ節以降の展開（tail）の長さを検証
    matched_phrase = match.group(0)
    idx = raw_text.find(matched_phrase)
    if idx < 0:
        tail_len = len(raw_text) - len(matched_phrase)
    else:
        tail_len = len(raw_text) - (idx + len(matched_phrase))
    # 全体長20文字以上なら後半12文字以上、全体長15文字以上なら後半8文字以上で保護
    if len(raw_text) >= 20 and tail_len >= 12:
        return True
    if len(raw_text) >= 15 and tail_len >= 8:
        return True
    return False



_CONTRAST_PREMISE_TSUKKOMI_KEYWORDS = (
    "言ってるのに",
    "言ってんのに",
    "書いてるのに",
    "聞いてるのに",
    "言ったのに",
    "聞いたのに",
    "すり替わっ",
    "すりかえ",
    "すり替え",
    "変わっちゃっ",
    "変わって",
    "前提",
    "自分から",
    "なんで",
    "何で",
    "なのに",
    "いくらなんでも",
    "無理やり",
    "無理矢理",
    "理論",
)


def _is_contrast_premise_tsukkomi_reply(
    assistant_text: str,
    overall_ratio: float,
) -> bool:
    """相手や返信元のフレーズを引用・対比して論理ツッコミを入れている正常な返答であるかを判定する。"""
    raw = str(assistant_text or "").strip()
    if len(raw) < 35 or overall_ratio >= 0.55:
        return False

    return any(kw in raw for kw in _CONTRAST_PREMISE_TSUKKOMI_KEYWORDS)


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
        and (len(raw_text) - len(first_sentence) >= 15)
    )
    is_greeting = (
        _is_greeting_or_introduction_clause(first_sentence)
        or _is_greeting_or_introduction_clause(first_clause)
        or _is_greeting_or_introduction_clause(raw_first_sentence)
        or _is_greeting_or_introduction_clause(raw_first_clause)
    )
    user_has_greeting = _is_greeting_or_introduction_clause(user)
    if (is_validation or is_acceptance or is_noun_reaction or (is_greeting and user_has_greeting)) and len(raw_text) >= 15:
        return False


    u_norm = _normalize_compare_text(user_text)
    a_norm = _normalize_compare_text(raw_text)
    overall_ratio = difflib.SequenceMatcher(None, u_norm, a_norm).ratio() if u_norm and a_norm else 0.0

    # 反問・ツッコミ（『〇〇？！』『〇〇…！？』等）で後半に展開がある場合はオウム返しから除外
    if _is_rhetorical_question_tsukkomi_reply(user_text, assistant_text, overall_ratio):
        return False


    # 前提すり替え・対比ツッコミ（『〇〇さんは「〜」って言ってるのに、なんで自分から〜にすり替わってるんですか』等）の保護
    if _is_contrast_premise_tsukkomi_reply(assistant_text, overall_ratio):
        return False


    # 文頭の名詞提示（例: 「鈴カステラ、〜」「〇〇……！〜」等）であり、
    # 返答全体が十分に長く（20文字以上）、全体の類似度が低い（0.50未満）場合はトピック受容・主題提示として保護
    if len(raw_text) >= 20 and overall_ratio < 0.50:
        raw_head = re.sub(
            r"(?:だよな|だろ|だよ|なのか|なの|だと|だな|って|とは|という|か|ね|な|だ)$",
            "",
            extract_effective_first_clause(assistant_text),
        ).strip()
        match_targets = [t for t in (raw_head, head) if t]
        for m_target in match_targets:
            if re.match(rf"^{re.escape(m_target)}[、,\s…!！?？はってもをがに]", effective_line):
                if not re.match(rf"^{re.escape(m_target)}[?？]+(?![!！])", effective_line):
                    return False


    if head in user:
        return True
    if len(head) < 4:
        return False


    longest = difflib.SequenceMatcher(None, head, user).find_longest_match(0, len(head), 0, len(user))
    return longest.a == 0 and longest.size >= max(4, int(len(head) * 0.50))


_SELF_PROJECTION_SUBJECT_RE = re.compile(
    r"^(?:私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち)も(?:[、,\s])*(?:毎回|いつも|よく|ついつい|つい|やっぱり|同じく|結構|わりと|だいたい|基本)?"
)
_SELF_PROJECTION_HABIT_ENDING_RE = re.compile(
    r"(?:"
    r"(?:派|タイプ|方|クチ|人間|たち|質)(?:なんですよね|なんですよ|んですよね|んですよ|なんです|ですよね|ですよ|ですね|だよ|だね|だわ|です|だ)|"
    r"(?:しちゃう|してしまう|してる|している|ちゃう|ておく|てみる)(?:派|タイプ|方|なんですよね|なんですよ|んですよね|んですよ|んです)"
    r")(?:[…!！?？。♪✨\s])*$"
)
_SELF_PROJECTION_GENERIC_WORDS = {
    "わたし", "あたし", "僕", "ぼく", "俺", "おれ", "自分", "うち", "毎回", "いつも", "ついつい", "やっぱり", "同じく",
    "結構", "わりと", "だいたい", "基本", "安心", "タイプ", "人間", "確認", "普通", "普段",
}
_CONTENT_STEM_RE = re.compile(r"[一-龠々]{2,}|[ァ-ヴー]{2,}|[A-Za-z0-9]{2,}")


def _looks_like_self_projection_mirror_echo(user_text: str, assistant_text: str) -> bool:
    """相手の表層的な行動ワードだけを拾い、『私も毎回〇〇して安心してる派なんですよ…！』のように
    主語を自分にすり替えて同調するだけの1文自己投影ミラーリング（実質オウム返し）を検出する。
    """
    raw_user = str(user_text or "").strip()
    raw_assistant = str(assistant_text or "").strip()
    if not raw_user or not raw_assistant:
        return False


    # 1. 長文や複数文で独自の展開がある場合は除外（短文1文のミラーリングのみ対象）
    if len(raw_assistant) > 55:
        return False

    effective_line = strip_leading_interjections(raw_assistant) or raw_assistant
    sentences = [s.strip() for s in re.split(r"[。!！?？\n]+", effective_line) if s.strip()]
    if len(sentences) > 1:
        return False


    # 2. 相手がAI自身の好み・属性・派閥を質問している場合（例: 「どっち派？」「君はどうしてる？」）は正常な回答なので除外
    if any(q in raw_user for q in ("?", "？", "何派", "どっち派", "どんなタイプ", "君は", "あなたは", "お前は", "どうしてる")):
        return False


    # 3. 文頭が「(一人称)も(毎回/いつも)...」で始まり、文末が「〜派/タイプなんですよ」等の属性・習慣構文であるか
    if not _SELF_PROJECTION_SUBJECT_RE.search(effective_line):
        return False

    if not _SELF_PROJECTION_HABIT_ENDING_RE.search(effective_line):
        return False


    # 4. アシスタント文中の内容語（2文字以上の漢字・カタカナ・英数字）を抽出し、相手の文の表層語の引き写ししか無いか判定
    body_without_subj = _SELF_PROJECTION_SUBJECT_RE.sub("", effective_line)
    assistant_tokens = [
        tok for tok in _CONTENT_STEM_RE.findall(body_without_subj)
        if tok not in _SELF_PROJECTION_GENERIC_WORDS and not re.fullmatch(r"(?:派|タイプ|方|クチ)", tok)
    ]
    if not assistant_tokens:
        return False


    user_lower = raw_user.lower()
    matched_count = 0
    unmatched_tokens: list[str] = []
    for tok in assistant_tokens:
        tok_l = tok.lower()
        # 完全一致、または2文字以上の語幹（例: 「見返」と「見直」の共通漢字や複合語の一部）がユーザー文に含まれるか
        if tok_l in user_lower or (len(tok_l) >= 2 and tok_l[:2] in user_lower):
            matched_count += 1
        elif len(tok_l) == 2 and tok_l[0] in user_lower and any(
            u_tok.startswith(tok_l[0]) for u_tok in _CONTENT_STEM_RE.findall(raw_user)
        ):
            # 「見直」→「見返」のような2文字複合動詞の後半1文字言い換えも表層ミラーリングとしてカウント
            matched_count += 1
        else:
            unmatched_tokens.append(tok)

    # 相手の表層語（例: 「手順書」「見直/見返」）をなぞっており、独自の新しい内容語がほぼ無い（0個）場合は自己投影エコーと判定
    if matched_count >= 1 and len(unmatched_tokens) == 0:
        log.warning(
            "[prompt-manager] _looks_like_self_projection_mirror_echo detected: user=%r assistant=%r",
            raw_user[:40],
            raw_assistant[:40],
        )
        return True

    return False



def looks_like_parrot_reply(user_text: str, assistant_text: str) -> bool:
    u = _normalize_compare_text(user_text)
    a = _normalize_compare_text(assistant_text)

    if not u or not a:
        return False


    if a == u:
        log.warning("[prompt-manager] looks_like_parrot_reply detected: exact match (text=%r)", a[:40])
        return True

    if _looks_like_self_projection_mirror_echo(user_text, assistant_text):
        return True

    # ---------------------------------------------------------
    # Hook Echo（文頭フレーズ反復＋抽象感想・ポエム）の検知
    # 例: 相手「〜プロレスの面が大きかったよね」 -> AI「プロレスの面…！あの頃の熱さって〜」
    # ---------------------------------------------------------
    raw_assistant = str(assistant_text or "").strip()
    # 冒頭の感嘆符・読点までのトークンを取得
    first_token_match = re.split(r"[…!！?？、,。\s]", raw_assistant)
    first_token = first_token_match[0].strip() if first_token_match else ""
    first_token_norm = _normalize_compare_text(first_token)

    # 相手テキストに含まれる3文字以上の名詞・フレーズを文頭で感嘆符反復しているか判定
    if len(first_token_norm) >= 3 and first_token_norm in u:
        if re.match(rf"^{re.escape(first_token)}[…!！]+", raw_assistant):
            # 後半が抽象的な懐古・共感ポエム（具体名詞・新情報なし）の場合にHook Echoと判定
            abstract_poem_re = re.compile(
                r"(?:熱さ|懐かし|恥ずかし|青春|時代|あの頃|確かに|分かります|そうですよね|もんね|ですね)"
            )
            if abstract_poem_re.search(raw_assistant):
                log.warning(
                    "[prompt-manager] looks_like_parrot_reply detected: hook echo + abstract poem (token=%r, text=%r)",
                    first_token,
                    raw_assistant[:40],
                )
                return True

    if len(u) < 5:
        return False


    matcher = difflib.SequenceMatcher(None, u, a)
    overall_ratio = matcher.ratio()
    # 全体丸写し・高類似度（語尾だけ変えたような復唱）は、受容句や承諾句があってもオウム返し判定
    if len(u) >= 15 and overall_ratio >= 0.85:
        log.warning(
            "[prompt-manager] looks_like_parrot_reply detected: high similarity ratio=%.2f (user=%r, assistant=%r)",
            overall_ratio,
            u[:30],
            a[:30],
        )
        return True

    # アシスタント返答が十分に長く（15〜20文字以上）、自然な受容・共感文（〜なんですね等）や
    # 承諾・遵守文（約束守るよ、気をつけるね等）、挨拶・自己紹介（初めまして、よろしく等）で
    # 後半に独立した展開がある場合は過剰検知を防止する
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
        and (len(raw_text) - len(first_sentence) >= 15)
    )
    is_greeting = (
        _is_greeting_or_introduction_clause(first_sentence)
        or _is_greeting_or_introduction_clause(first_clause)
        or _is_greeting_or_introduction_clause(raw_first_sentence)
        or _is_greeting_or_introduction_clause(raw_first_clause)
    )
    user_has_greeting = _is_greeting_or_introduction_clause(user_text)
    if (is_validation or is_acceptance or is_noun_reaction or (is_greeting and user_has_greeting)) and len(raw_text) >= 15:
        return False


    # 反問・ツッコミ（『〇〇？！』『〇〇…！？』等）で後半に展開がある場合はオウム返しから除外
    if _is_rhetorical_question_tsukkomi_reply(user_text, assistant_text, overall_ratio):
        return False


    # 前提すり替え・対比ツッコミ（『〇〇さんは「〜」って言ってるのに、なんで自分から〜にすり替わってるんですか』等）の保護
    if _is_contrast_premise_tsukkomi_reply(assistant_text, overall_ratio):
        return False


    # 文頭の名詞提示（例: 「鈴カステラ、〜」「〇〇……！〜」等）であり、
    # 返答全体が十分に長く（20文字以上）、全体の類似度が低い（0.50未満）場合はトピック受容・主題提示として保護
    if len(raw_text) >= 20 and overall_ratio < 0.50:
        raw_head = re.sub(
            r"(?:だよな|だろ|だよ|なのか|なの|だと|だな|って|とは|という|か|ね|な|だ)$",
            "",
            extract_effective_first_clause(assistant_text),
        ).strip()
        head_frag = _opening_echo_fragment(assistant_text)
        match_targets = [t for t in (raw_head, head_frag) if t]
        for m_target in match_targets:
            if re.match(rf"^{re.escape(m_target)}[、,\s…!！?？はってもをがに]", effective_line):
                if not re.match(rf"^{re.escape(m_target)}[?？]+(?![!！])", effective_line):
                    return False


    if _looks_like_opening_topic_echo(user_text, assistant_text):
        return True

    # ユーザー発言が短い単語・名詞（20文字以下）の場合、AI返答が十分に長く（20文字以上）
    # かつ先頭エコーでないなら、文中に話題の単語が含まれていてもオウム返しと判定しない
    if len(u) <= 20 and len(a) >= 20 and not _looks_like_opening_topic_echo(user_text, assistant_text):
        return False


    if u in a:
        if len(a) < int(len(u) * 1.8) or _looks_like_opening_topic_echo(user_text, assistant_text):
            return True

    matcher = difflib.SequenceMatcher(None, u, a)
    longest_match = matcher.find_longest_match(0, len(u), 0, len(a))
    longest_ratio = longest_match.size / len(u)
    if longest_ratio >= 0.50:
        if _is_contrast_premise_tsukkomi_reply(assistant_text, overall_ratio):
            return False

        if _looks_like_opening_topic_echo(user_text, assistant_text) or (longest_match.size / len(a)) >= 0.40:
            return True

    # 助詞・助動詞（2文字）の偶然の一致による誤判定を防ぐため block.size >= 3 で判定
    reused_chars = sum(block.size for block in matcher.get_matching_blocks() if block.size >= 3)
    reused_ratio = reused_chars / len(u)
    if len(u) >= 8 and reused_ratio >= 0.55:
        if _is_contrast_premise_tsukkomi_reply(assistant_text, overall_ratio):
            return False

        if _looks_like_opening_topic_echo(user_text, assistant_text) or (reused_chars / len(a)) >= 0.45:
            return True

    return False



_PEER_PERSONA_TAIL_STRIP_RE = re.compile(
    r"(?:なんですよ|なのですよ|なんですね|なのですね|ですよ|ですね|ますよ|ますね|"
    r"じゃないですか|じゃないですかね|でしょうか|ですかね|ですよね|ますよね|"
    r"だよね|だな|だろな|だろうな|だろ|だろう|じゃん|わな|かな|かも|わ|よ|ね|な|ぞ|ぜ)+$"
)
_PEER_HEDGING_SYNONYM_MAP: tuple[tuple[str, str], ...] = (
    ("下がってる", "落ちてる"),
    ("下がっている", "落ちている"),
    ("無理だろ", "無理な話"),
    ("無理だろう", "無理な話"),
    ("無理だ", "無理な話"),
    ("さすがにもう", "さすがに"),
    ("流石にもう", "さすがに"),
    ("さすがに", "流石に"),
)


def _normalize_peer_core_clause(text: str) -> str:
    """他ユーザー発言とAI返答のコア骨格比較用に、ペルソナ語尾や表記揺れを正規化する。"""
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"^(?:いやー?|うーん|まあ|まぁ|えっと|あの|てか|つか|というか|でも|けど|ただ|ほんと|本当)[、,\s]*", "", s)
    for src, dst in _PEER_HEDGING_SYNONYM_MAP:
        s = s.replace(src, dst)
    s = re.sub(r"[…・!！?？。、,.\s「」『』（）()]+", "", s)
    s = _PEER_PERSONA_TAIL_STRIP_RE.sub("", s)
    return _normalize_compare_text(s)


_CONTEXT_LEADING_TIMESTAMP_RE = re.compile(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)+")
_SPEAKER_RELATION_SUFFIX_RE = re.compile(
    r"\s*(?:\((?:>>[^)]*|独立発言|[^)]*への返信)\)|\[(?:>>[^\]]*|独立発言|[^\]]*への返信)\])\s*$"
)


def _extract_peer_utterance_from_context_line(
    line: str,
    target_user_norm: str = "",
    *,
    bot_aliases: Sequence[str] = (),
) -> tuple[Optional[str], str]:
    """Extract (speaker_name, utterance_text) from a formatted context line.

    Returns (None, "") if the line belongs to the bot/assistant/system or has no body.
    """
    raw = str(line or "").strip()
    if not raw or raw.startswith("※"):
        return None, ""
    # Strip leading timestamp(s) like "[14:05] " before splitting on ":"
    raw = _CONTEXT_LEADING_TIMESTAMP_RE.sub("", raw).strip()
    if not raw:
        return None, ""
    parts = re.split(r"[:：]", raw, maxsplit=1)
    if len(parts) < 2:
        return None, ""
    speaker_part = parts[0].strip()
    body = parts[1].strip()
    if not body:
        return None, ""
    # Strip leading stray timestamp/speaker prefix if body still starts with it, and skip internal memos
    body = _CONTEXT_LEADING_TIMESTAMP_RE.sub("", body).strip()
    if body.startswith("(内心のメモ") or body.startswith("（内心のメモ"):
        return None, ""
    # Strip relation suffixes like "User (>>Assistant)", "Assistant (独立発言)", "User [>>Bot への返信]"
    speaker_clean = speaker_part
    while True:
        next_clean = _SPEAKER_RELATION_SUFFIX_RE.sub("", speaker_clean).strip()
        if next_clean == speaker_clean:
            break
        speaker_clean = next_clean
    speaker_clean = speaker_clean.strip("「」『』\"'[]()")
    if not speaker_clean:
        return None, ""
    speaker_lower = speaker_clean.lower()
    if speaker_lower in ("assistant", "ai", "bot", "system"):
        return None, ""
    for alias in bot_aliases:
        alias_clean = str(alias or "").strip().lower()
        if alias_clean and (speaker_lower == alias_clean or alias_clean in speaker_lower):
            return None, ""
    # Strip trailing reaction annotation like " [リアクション: 👍×1]"
    body = re.sub(r"\s*\[(?:Bot)?リアクション:[^\]]*\]\s*$", "", body).strip()
    body = re.sub(r"\s*（メディア補足:.*?）\s*$", "", body).strip()
    # 返信引用プレフィックス（（〇〇の「...」への返信）等）を除去
    body = re.sub(r"^（[^）]*への返信）\s*", "", body).strip()
    if not body:
        return None, ""
    # 対象メッセージ本文と完全一致する行は対象メッセージ自身の履歴行なので除外
    if target_user_norm and _normalize_compare_text(body) == target_user_norm:
        return None, ""
    return speaker_clean, body


def looks_like_context_peer_plagiarism(
    user_text: str,
    assistant_text: str,
    context_lines: Sequence[str] | None = None,
) -> bool:
    """会話履歴（context_lines）内の他ユーザー発言の語尾変えパクり・複数人発言のキメラ合体を検知する。"""
    if not context_lines or not assistant_text:
        return False


    target_norm = _normalize_compare_text(user_text)
    target_core = _normalize_peer_core_clause(user_text)

    raw_reply = str(assistant_text or "").strip()
    reply_clauses = [
        _normalize_peer_core_clause(c)
        for c in re.split(r"[\n。!！?？…]+", raw_reply)
        if len(_normalize_peer_core_clause(c)) >= 5
    ]
    reply_full_core = _normalize_peer_core_clause(raw_reply)
    if not reply_clauses or not reply_full_core:
        return False


    peer_cores: list[str] = []
    for line in context_lines:
        peer_speaker, body = _extract_peer_utterance_from_context_line(line, target_norm)
        if not body or not peer_speaker:
            continue
        p_core = _normalize_peer_core_clause(body)
        if len(p_core) < 6:
            continue
        # 対象メッセージ（user_text）自体にほぼそのまま含まれる内容は除外
        if target_core and p_core in target_core:
            continue
        if p_core not in peer_cores:
            peer_cores.append(p_core)

    if not peer_cores:
        return False


    matched_peer_indices: set[int] = set()
    for idx, p_core in enumerate(peer_cores):
        for r_clause in reply_clauses:
            matcher = difflib.SequenceMatcher(None, p_core, r_clause)
            match = matcher.find_longest_match(0, len(p_core), 0, len(r_clause))
            longest = match.size
            common_substr = p_core[match.a : match.a + longest]
            # 共通部分が対象メッセージ（user_text）由来の単語だけなら除外
            if target_core and longest > 0 and common_substr in target_core:
                continue
            ratio = matcher.ratio()
            # 1) 単一発言の語尾変え丸パクり（8文字以上のコア一致、または6文字以上かつ類似度0.72以上）
            if longest >= 8 or (longest >= 6 and ratio >= 0.72):
                log.warning(
                    "[prompt-manager] looks_like_context_peer_plagiarism detected single-peer theft: peer=%r reply_clause=%r (longest=%d, ratio=%.2f)",
                    p_core[:40],
                    r_clause[:40],
                    longest,
                    ratio,
                )
                return True
            # 2) キメラ合体構成要素のカウント（5文字以上の固有フレーズ一致、または類似度0.58以上）
            if longest >= 5 or ratio >= 0.58:
                matched_peer_indices.add(idx)

    # 複数の異なる他ユーザー発言のフレーズを1つの返答にパッチワーク合体させた場合
    if len(matched_peer_indices) >= 2:
        log.warning(
            "[prompt-manager] looks_like_context_peer_plagiarism detected multi-peer chimera: matched_peers=%d reply=%r",
            len(matched_peer_indices),
            raw_reply[:60],
        )
        return True

    return False



_MISDIRECTED_CONFUSION_RE = re.compile(
    r"(?:なんで|なぜ|どうして)\s*(?:いきなり|急に|突然)"
    r"|(?:何のことだか|なんのことだか|何の話だか|なんの話か).{0,8}(?:分かりません|わかりません|分からない|わからない)"
    r"|(?:いきなり|急に|突然).{0,12}(?:投げて|振って|言われて)(?:くる|こない|も)"
)


def looks_like_misdirected_confusion_reply(reply: str, *, is_reply_to_other_user: bool) -> bool:
    """他ユーザー宛て返信への応答で、自分宛てと誤認した困惑・無知表明をしていれば True。"""
    if not is_reply_to_other_user:
        return False

    return bool(_MISDIRECTED_CONFUSION_RE.search(str(reply or "")))


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



# 日本語（常用・人名・JIS漢字）では使われない簡体字専用文字のパターン
_SIMPLIFIED_CHINESE_CHARS_RE = re.compile(
    r"["
    r"劲儿这们吗对说么过还让给欢买卖车东马鸟鸡鱼门间问闻关开"
    r"电飞风见观视话语读书认识记设议论讲爱亲觉惊叹乐忧恼师"
    r"孙爷奶妈爸业产务动劳办庆应庙废亿两够现边远进运连迟适"
    r"选际阳阴队阶陆陈险实头长"
    r"]"
)

# 日中同形漢字を含んでいても中国語文法・語彙として混入しやすいフレーズ（例: 劲儿, 为什么, 怎么, 觉得, 已经 等）
_CHINESE_PHRASE_LEAK_RE = re.compile(
    r"(?:"
    r"劲儿|一点儿|一会儿|这儿|那儿|哪儿|"
    r"为什么|怎么(?:样|办|回事)?|什么(?:意思|时候)?|"
    r"觉得|已经|虽然|但是|因为|(?<![のたる])所以|如果|或者|而且|"
    r"可以|没有|不是|就是|还是|真是|太好了|"
    r"大家(?:好|都)|非常(?:感謝|感谢|好)|"
    r"[一-龠]{1,4}了吗|[一-龠]{1,4}了吧|[一-龠]{1,4}了呢"
    r")"
)


def contains_chinese_leak(text: str) -> bool:
    """テキスト内に中国語（簡体字専用漢字または中国語固有フレーズ）が混入しているか判定する（英語は許容）。"""
    raw = str(text or "").strip()
    if not raw:
        return False

    # URLやメンションは除外して判定
    cleaned = re.sub(r"https?://\S+|<@!?\d+>|<#\d+>", "", raw)
    if _SIMPLIFIED_CHINESE_CHARS_RE.search(cleaned):
        return True
    if _CHINESE_PHRASE_LEAK_RE.search(cleaned):
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

    if contains_chinese_leak(scrubbed):
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



def looks_like_abnormal_assistant_reply(text: str, *, user_text: str = "") -> bool:
    t = str(text or "").strip()
    if not t:
        return True
    if (
        looks_like_prompt_leak(t)
        or looks_like_reasoning_leak(t)
        or looks_like_multi_turn_output(t)
        or looks_like_non_japanese_reply(t)
        or looks_like_unusable_assistant_reply(t)
        or looks_like_mimicked_kaomoji_or_hanging_tail(t, user_text=user_text)
        or looks_like_false_style_accusation(t, user_text=user_text)
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
        clauses = [last_line.strip()]

    from lib.config_utils import cfg
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    pattern = (
        r"(?:なんですよね|なんですよ|んですよね|んですよ|なんです|ですよね|"
        r"ました|でした|だった|かった|ですよ|ですね|ますよ|ますね|です|ます|"
        r"だよ|だね|なのだ|のだ|のよ|わよ|わね|ぜ|ぞ|さ|ね|よ|な|か)+$"
    )

    def _strip_clause_core(c_text: str) -> str:
        cn = _normalize_compare_text(c_text)
        if first_person:
            cn = re.sub(rf"^(?:{re.escape(first_person)}|私|あたし|自分|ぼく|僕)[はもの]", "", cn)
        else:
            cn = re.sub(r"^(?:私|あたし|自分|ぼく|僕)[はもの]", "", cn)
        return re.sub(pattern, "", cn)

    accum = ""
    for i in range(len(clauses) - 1, -1, -1):
        accum = clauses[i] + accum
        core = _strip_clause_core(accum)
        if len(core) >= 8:
            return core
    return _strip_clause_core(accum)


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

    # 段落単位での前回Bot発言コピー検知（後半段落や一部段落の丸写し）
    new_paragraphs = [p.strip() for p in re.split(r"\n+", str(new_reply or "")) if p.strip()]
    prev_paragraphs = [p.strip() for p in re.split(r"\n+", str(previous_reply or "")) if p.strip()]
    for p in new_paragraphs:
        pn = _normalize_compare_text(p)
        if len(pn) >= 18:
            if pn in prev_n:
                return True
            for pp in prev_paragraphs:
                ppn = _normalize_compare_text(pp)
                if len(ppn) >= 18 and difflib.SequenceMatcher(None, pn, ppn).ratio() >= 0.85:
                    return True

    # 結びの文・オチ節の金太郎飴検知（ある程度長さのある返答同士で比較）
    if len(prev_n) >= 15 and len(new_n) >= 15:
        c1 = _extract_final_clause_core(previous_reply or "")
        c2 = _extract_final_clause_core(new_reply or "")
        if (len(c1) >= 4 and len(c2) >= 4 and c1 == c2) or (len(c1) >= 6 and len(c2) >= 6):
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
            log.warning("[prompt-manager] looks_like_character_deviation detected: forbidden pronoun %r in %r", word, r[:50])
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
            log.warning("[prompt-manager] looks_like_character_deviation detected: abusive word %r in %r", word, r[:50])
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
            log.warning("[prompt-manager] looks_like_character_deviation detected: aggressive tail in %r", r[:50])
            return True

    # 4. 相手に対する異常者扱い・ヒステリックな対話拒絶（非常識、怖いですから言わないで等）
    if looks_like_abnormal_rejection(r):
        log.warning("[prompt-manager] looks_like_character_deviation detected: abnormal rejection in %r", r[:50])
        return True

    return False



ABNORMAL_REJECTION_PATTERNS: tuple[str, ...] = (
    # 1. 非常識扱い・非難
    r"非常識(?:なこと|な人|扱い|ですよ|です|ですね|言わないで|言わんで)",
    r"常識(?:が|は)?(?:ない|なさすぎ|ないんですか|ないんですかね)",
    # 2. 怖いですから(もう)?(言わないで|やめて|話しかけないで)
    r"怖い(?:ですから|だから|ですし)[、，\s]*(?:もう)?(?:言わないで|言わんで|やめて|話しかけないで|やめてください|言わないでください)",
    # 3. 言わないでください、怖いですから / やめてください、怖いですから
    r"(?:言わないで|言わんで|やめて)(?:ください|よ)?[、，\s]*(?:もう)?怖い(?:ですから|だから|ですし)",
    # 4. 頭おかしい / 異常ですよ / 気は確か
    r"頭(?:が|は)?(?:おかしい|変|狂って)",
    r"異常(?:ですよ|です|ですね|扱い)",
    r"気(?:は|でも)?(?:確か|狂った)",
    # 5. 相手の自然な確認・発言に対する理不尽な意味勘違い難癖・逆ギレ
    r"(?:言っ|話して)る意味(?:を)?勘違いして(?:ない|ません|る)",
    r"(?:勘違い|誤解)(?:してないですか|してませんか|しないでください)",
)


def looks_like_abnormal_rejection(reply: str) -> bool:
    """
    返答が相手のボケや発言に対して『非常識』と見下して非難したり、
    『怖いですから言わないでください』とヒステリックに対話を拒絶・逃走しているかを判定する。
    """
    r = str(reply or "").strip()
    if not r:
        return False

    return any(bool(re.search(pat, r)) for pat in ABNORMAL_REJECTION_PATTERNS)


LANGUAGE_CAPABILITY_PATTERNS: tuple[str, ...] = (
    # 日本語しか〜（わからない/話せない/使えない/対応していない 等）
    r"(?:日本語|にほんご)(?:しか|だけ|のみ)(?:わから|分から|話せ|喋れ|しゃべれ|理解でき|使え|対応して|無理|ダメ|だめ)",
    # 英語/外国語/他言語 は〜（わからない/話せない/読めない 等）
    r"(?:英語|えいご|外国語|他言語|日本語以外|他の言葉)(?:は|が|なんて|など|とか)?(?:わから|分から|話せ|読め|理解でき|喋れ|しゃべれ|使え|対応でき)",
    # 日本語で話して / 日本語ならわかる
    r"(?:日本語|にほんご)(?:で|なら|表記で)(?:話して|言って|書いて|頼む|お願い|お話し|わかる|分かる)",
)

INCOMPREHENSION_REPLY_PATTERNS: tuple[str, ...] = (
    # 1. 意味・意図がつかめない・わからない・取れない
    r"(?:意味|意図)(?:が|は|も)?(?:ちょっと|よく|さっぱり|全然|まったく|あまり|イマイチ|いまいち)?(?:わから|分から|つかめ|掴め|取れ|とれ|取りづら|とりづら|取れない|理解でき|見え|測りかね)",
    # 2. 何のこと・何を言っているのか・何がどう わからない
    r"何(?:のこと|を言っ|を話して|がどう|のことだか)(?:のか|んだか|のかさっぱり|のかよく)?(?:.*)?(?:わから|分から|理解でき)",
    # 3. 〜と言われても / 聞かれても / 〜られても わからない・意味がつかめない・知らない
    r"(?:言われても|聞かれても|言われましても|話されても|[ら|れ]れても)(?:.*)?(?:わから|分から|理解でき|つかめ|掴め|知ら)",
    # 4. 突然のことで / いきなり〜で わからない・意味がつかめない
    r"(?:突然|いきなり|急)(?:のことで|なことで|のことすぎて)?(?:.*)?(?:意味(?:が|は)?(?:つかめ|掴め|わから|分から)|(?:さっぱり|よく|ちょっと)?(?:わから|分から))",
    # 5. 〜について / 〜のことは (よく/さっぱり/全然) わからない
    r"(?:について|に関しては|のことは|の件は|は)(?:.*)?(?:よく|さっぱり|全然|まったく|サッパリ|ちっとも)(?:わから|分から|知ら|理解でき)",
    # 6. 一人称/キャラ名 には (ちょっと/よく/さっぱり) (意味が/意図が)? わからない/つかめない
    r"(?:(?:私|わたし|僕|ぼく|俺|おれ|自分|うち)|[ぁ-んァ-ヶ一-龠A-Za-z0-9_]{1,10})には(?:.*)?(?:よく|さっぱり|全然|まったく|ちょっと)?(?:意味が|意図が)?(?:わから|分から|つかめ|掴め)",
    # 7. さっぱりわからない / 全然わからない / よくわからない (文末・言い切り)
    r"(?:さっぱり|全然|まったく|サッパリ|ちっとも)(?:わから|分から|知ら|理解でき)(?:ない|ねェ|ん|ないんですよ|ないんだ|ないです|ません|ぬ)",
    # 8. わけがわからない
    r"(?:わけ|訳|ワケ)が(?:わから|分から)",
    # 9. 理解できない / 理解が追いつかない
    r"(?:さっぱり|全然|まったく|ちょっと)?理解(?:が追いつ|でき|いたしかね)",
    # 10. 単語・発言に対する聞き返し・困惑・逆質問（何ですか・何ですよ等）
    r"[「『][^「」『』\n]{1,40}[」』]って何(?:ですよ|ですか|なんだ|なの|だよ|かしら|ね)",
    r"何(?:を言っ|のこと言っ|のこと話して)(?:て|た)?(?:る|のですか|んですか)",
    r"何を言い出すんですか",
    r"[「『][^「」『』\n]{1,40}[」』]って(?:どういう意味|何のこと)",
)

UNKNOWN_WORD_INCOMPREHENSION_PATTERNS: tuple[str, ...] = (
    # 『〇〇』って何ですか / 『〇〇』って何ですよ / 「〇〇」ってどういう意味 など
    r"[「『]([^「」『』\n]{1,30})[」』]って(?:何|なに|どういう意味|何のこと|何なんだ|誰)",
    r"[「『]([^「」『』\n]{1,30})[」』]って何(?:ですよ|ですか|なんだ|なの|かしら|だよ|ね)",
    r"[「『]([^「」『』\n]{1,30})[」』](?:とは|って誰|ってなに)",
)


def looks_like_unknown_word_incomprehension(reply: str) -> bool:
    """生成された返信が、ユーザーの単語に対して『何ですか』『どういう意味ですか』等と聞き返して不知・困惑を表明しているかを判定する。"""
    r = str(reply or "").strip()
    if not r:
        return False

    return any(bool(re.search(pat, r)) for pat in UNKNOWN_WORD_INCOMPREHENSION_PATTERNS)


def extract_unknown_incomprehension_word(reply: str) -> str:
    """不知・困惑を表明している返答から、対象となっている単語（カギ括弧内等）を抽出する。"""
    r = str(reply or "").strip()
    if not r:
        return ""
    for pat in UNKNOWN_WORD_INCOMPREHENSION_PATTERNS:
        m = re.search(pat, r)
        if m and m.group(1):
            w = m.group(1).strip()
            if w:
                return w
    return ""


def looks_like_unknown_or_incomprehension_reply(
    reply: str,
    *,
    user_text: str = "",
) -> bool:
    """
    返答がユーザーの発言に対して理解不能・意味不明・「わからない」と困惑しているかを判定する。
    ただし言語能力に関する言及（「日本語しかわからない」等）や親切な案内、共感の二重否定は除外する。
    """
    r = str(reply or "").strip()
    if not r:
        return False


    from lib.config_utils import cfg

    # 1. 言語対応・能力の言及（日本語しかわからない等）はスキップ対象外
    if any(re.search(pat, r) for pat in LANGUAGE_CAPABILITY_PATTERNS):
        return False


    # 2. 親切な質問案内（「わからないことがあったら聞いてね」等）はスキップ対象外
    if re.search(r"(?:わから|分から)ない(?:こと|点|ところ|部分)(?:が|は)?(?:あれ|あっ|たら)", r):
        return False


    # 3. 二重否定による共感（「わからなくもない」等）はスキップ対象外
    if re.search(r"わからなく[もは]ない|わからない[でわけ][もは]ない", r):
        return False


    # 4. コンフィグによるカスタム除外パターン
    custom_exclude = str(cfg("SKIP_UNKNOWN_REPLY_EXCLUDE_PATTERNS", "") or "").strip()
    if custom_exclude and re.search(custom_exclude, r):
        return False


    # 5. コンフィグによるカスタム追加パターン
    custom_extra = str(cfg("SKIP_UNKNOWN_REPLY_EXTRA_PATTERNS", "") or "").strip()
    if custom_extra and re.search(custom_extra, r):
        return True

    # 6. 基本の理解不能・不知パターン照合
    return any(bool(re.search(pat, r)) for pat in INCOMPREHENSION_REPLY_PATTERNS)


def should_skip_unknown_reply(
    reply: str,
    *,
    user_text: str = "",
    is_primary_channel: bool = False,
    is_direct: bool = False,
    force_response: bool = False,
) -> bool:
    """
    設定 SKIP_UNKNOWN_REPLY_ENABLED が有効で、返答が理解不能・「わからない」系である場合に
    返信をスキップすべきかを判定する。
    ただし、OLLAMA_CHANNEL_ID で指定されたチャンネル（is_primary_channel=True）や、
    メンションなどで直接話しかけられた場合（is_direct=True または force_response=True）は
    例外としてスキップせず返信する。
    """
    from lib.config_utils import cfg_bool
    if not cfg_bool("SKIP_UNKNOWN_REPLY_ENABLED", True):
        return False

    if is_primary_channel or is_direct or force_response:
        return False

    return looks_like_unknown_or_incomprehension_reply(reply, user_text=user_text)


def looks_like_mimicked_kaomoji_or_hanging_tail(
    reply: str,
    *,
    user_text: str = "",
) -> bool:
    """サニタイズ後もなお相手固有の顔文字や不自然な語尾エコーが残存しているかを判定する。"""
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r:
        return False

    if u:
        from .cleaner import extract_kaomoji_candidates
        user_kaomojis = extract_kaomoji_candidates(u)
        try:
            from lib.config_utils import cfg
            allowed = set(cfg("ALLOWED_KAOMOJI", []) or [])
        except Exception:
            allowed = set()
        for k in user_kaomojis:
            if k not in allowed and k in r:
                return True
    # ぶら下がり語尾（接続助詞＋さあ/さ等）の残存チェック
    if re.search(
        r"(?:なんですけど|んですけど|だけど|ですが|だが|けれど|けれども|のに)[、，\s]*(?:さあ|さ)[!！…\.\?？~〜\s]*$",
        r,
    ):
        return True
    # 単独のぶら下がり「、さあ」「、さ」が文末に残っている場合
    if re.search(r"[、，\s]+(?:さあ|さ)[!！…\.\?？~〜\s]*$", r):
        return True
    return False



def looks_like_false_style_accusation(
    reply: str,
    *,
    user_text: str = "",
) -> bool:
    """サニタイズ後もなお相手が使っていない顔文字や語尾への冤罪指摘が残存しているかを判定する。"""
    r = str(reply or "").strip()
    u = str(user_text or "").strip()
    if not r or not u:
        return False


    from .cleaner import (
        _FALSE_KAOMOJI_SINGLE_PATTERN,
        _FALSE_QUOTE_TAIL_PATTERN,
        _FALSE_STYLE_COMPOUND_PATTERN,
        _is_preceded_by_third_party_subject,
        _normalize_tail_quote_term,
        _user_has_kaomoji_or_emoji,
    )

    user_has_kaomoji = _user_has_kaomoji_or_emoji(u)
    if not user_has_kaomoji:
        for m in _FALSE_STYLE_COMPOUND_PATTERN.finditer(r):
            if not _is_preceded_by_third_party_subject(r, m.start()):
                return True
        for m in _FALSE_KAOMOJI_SINGLE_PATTERN.finditer(r):
            if not _is_preceded_by_third_party_subject(r, m.start()):
                return True

    for m in _FALSE_QUOTE_TAIL_PATTERN.finditer(r):
        if not _is_preceded_by_third_party_subject(r, m.start()):
            quote_term = _normalize_tail_quote_term(m.group(2))
            if quote_term and quote_term not in u:
                return True

    return False



# 既知・好意・体験済みの表現パターン
_FAVORITE_OR_EXPERIENCED_PATTERNS: tuple[str, ...] = (
    r"(?:大好[き物]|好[き物]|愛用|大ファン|お気に入り)",
    r"(?:食べたこと(?:が|は)?ある|よく食べ[るます]|食べ[たてまし]|美味しかった|おいしかった)",
    r"(?:行ったこと(?:が|は)?ある|よく行[くきます]|行っ[たてまし]|訪問し[たて])",
)

_KNOWN_SUBJECT_PATTERNS: tuple[str, ...] = (
    r"(?:知って[るいます]|知ってた|当然知って|もちろん知って|有名[だです])",
)

# 前言撤回・未体験・不知の表現パターン
_INEXPERIENCED_OR_TASTE_QUERY_PATTERNS: tuple[str, ...] = (
    r"(?:本当に|ほんとに)?(?:美味しい|おいしい)(?:の|んですか|んですかね|のか|のですか|のかしら|のか…|の…)[！？!?…\.\s]*",
    r"(?:いつか|今度|そのうち|一度は)?(?:食べてみたい|行ってみてえ|行ってみたい|試してみたい|味わってみたい)",
    r"(?:食べたこと(?:が|は)?ない|行ったこと(?:が|は)?ない|行っ[たて]ことない|食べ[たて]ことない)",
    r"(?:どんな味|どんな感じ|どんなお店)(?:なんですか|なの|なのか|ですか)",
)

_UNKNOWN_DENIAL_PATTERNS: tuple[str, ...] = (
    r"(?:全然|さっぱり|まったく|よく)?(?:知らない|知らなくて|聞いたことない|知らなかった)",
)

# 否定・嫌悪の表現パターン
_DISLIKE_OR_DENIAL_PATTERNS: tuple[str, ...] = (
    r"(?:大嫌い|嫌い(?:ですよ|です|だ|なんですよ|なんだ)|好きじゃない|好きではありません|苦手なんですよ|苦手です|受け付けない)",
)


def looks_like_self_contradicting_reply(
    reply: str,
    *,
    last_assistant_text: str = "",
    user_text: str = "",
) -> bool:
    """直前の自己発言（スタンス）と今回の返答が明白に矛盾・前言撤回しているかを判定する。

    例: 直前で『大好き』『食べた』『知っている』と答えていた対象に対して、
    今回の返答で『本当に美味しいんですか…！いつか食べてみたい』『食べたことない』『知らない』や
    『嫌いですよ…！』と未体験・不知・嫌悪を装って前言撤回している場合を検知する。
    """
    r = str(reply or "").strip()
    last_a = str(last_assistant_text or "").strip()
    u = str(user_text or "").strip()
    if not r or not last_a:
        return False


    # 1. ユーザーからの訂正・指摘に対する素直な訂正や受容は除外（過剰検知防止）
    if u and re.search(r"(?:違う|ちがう|じゃない|違く|勘違い|誤解|嘘|うそ)", u):
        if re.search(r"(?:すみません|ごめんなさい|勘違い|間違え|失礼|そうだったんですね|知らなかった|ぴゃ|わわ)", r):
            return False


    # 2. 短い単語ツッコミや感嘆（15文字以下）で、前言撤回の述語（嫌い、未体験、不知）を含まないものは除外
    has_dislike_reply = any(re.search(pat, r) for pat in _DISLIKE_OR_DENIAL_PATTERNS)
    has_inexp_reply = any(re.search(pat, r) for pat in _INEXPERIENCED_OR_TASTE_QUERY_PATTERNS)
    has_unknown_reply = any(re.search(pat, r) for pat in _UNKNOWN_DENIAL_PATTERNS)

    if not (has_dislike_reply or has_inexp_reply or has_unknown_reply):
        if len(r) <= 15 and _is_short_noun_reaction_clause(r):
            return False


    # 3. 直前発言が好意・体験済みスタンスを持っているか
    has_favorite_or_exp = any(re.search(pat, last_a) for pat in _FAVORITE_OR_EXPERIENCED_PATTERNS)

    if has_favorite_or_exp and has_inexp_reply:
        log.warning(
            "[prompt-manager] looks_like_self_contradicting_reply detected: favorite/exp contradicted (last=%r, reply=%r)",
            last_a[:40],
            r[:40],
        )
        return True

    # 4. 直前発言が既知スタンスを持っているか
    has_known = any(re.search(pat, last_a) for pat in _KNOWN_SUBJECT_PATTERNS)

    if has_known and has_unknown_reply:
        log.warning(
            "[prompt-manager] looks_like_self_contradicting_reply detected: known stance contradicted (last=%r, reply=%r)",
            last_a[:40],
            r[:40],
        )
        return True

    # 5. 直前発言が好意・体験スタンスを持っているのに対し、今回返答が嫌い・否定の前言撤回をしているか
    if has_favorite_or_exp and has_dislike_reply:
        log.warning(
            "[prompt-manager] looks_like_self_contradicting_reply detected: favorite stance contradicted with dislike (last=%r, reply=%r)",
            last_a[:40],
            r[:40],
        )
        return True

    return False


_PERSON_FULLNAME_RE = re.compile(r"[ァ-ヶー]{2,}・[ァ-ヶー]{2,}")
_KATAKANA_TOKEN_RE = re.compile(r"[ァ-ヶー]{2,}")
_PERSON_CONTEXT_PATTERN = re.compile(r"(?:の二番煎じ|みたいな|級の|選手|投手|打者|監督|コーチ)")


def find_ungrounded_person_names(
    comment: str,
    allowed_terms: set[str],
    *,
    stopwords: set[str] | None = None,
    persona_names: set[str] | None = None,
) -> list[str]:
    """生成文から、コンテキスト（allowed_terms）にグラウンディングされていない人名・固有名詞を検出する。
    - パターンA（フルネーム）: カタカナの中黒連結名（例: アダム・リゲイ）。連結全体が allowed_terms に無く、
      かつ構成要素の両方とも allowed_terms に無い場合は違反。
    - パターンB（単独名＋人物文脈）: カタカナ語で、同一文内に人物文脈パターン（の二番煎じ、選手、監督等）と共起し、
      allowed_terms、stopwords、persona_names のいずれにも含まれない場合は違反。
    """
    raw = str(comment or "").strip()
    if not raw:
        return []

    # 引用行を除外した本文を対象とする
    non_quote_lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith(">")]
    body = "\n".join(non_quote_lines)
    if not body:
        return []

    stop_words = stopwords or set()
    p_names = persona_names or set()
    allowed_norm = {t.lower().replace("・", "") for t in allowed_terms}
    allowed_raw = set(allowed_terms)

    violations: list[str] = []
    seen: set[str] = set()

    # パターンA: カタカナ・フルネーム（中黒区切り）
    for m in _PERSON_FULLNAME_RE.finditer(body):
        full_name = m.group(0).strip()
        if full_name in allowed_raw or full_name.lower() in allowed_norm:
            continue
        parts = full_name.split("・")
        # 構成要素の両方がコンテキストにあれば許容（例: 「マギー」と「ジェリー」が共にあればマギー・ジェリーは許容）
        if all(p in allowed_raw or p.lower() in allowed_norm for p in parts):
            continue
        if full_name not in seen:
            seen.add(full_name)
            violations.append(full_name)

    # パターンB: 文ごとの人物文脈共起チェック
    sentences = re.split(r"(?<=[。！？!?\n])", body)
    for s in sentences:
        s_clean = s.strip()
        if not s_clean or not _PERSON_CONTEXT_PATTERN.search(s_clean):
            continue
        for km in _KATAKANA_TOKEN_RE.finditer(s_clean):
            token = km.group(0).strip()
            if len(token) < 2:
                continue
            if (
                token in allowed_raw
                or token.lower() in allowed_norm
                or token in stop_words
                or token in p_names
            ):
                continue
            # フルネームの部分文字列として既に違反登録されていれば重複追加しない
            if any(token in v for v in violations):
                continue
            if token not in seen:
                seen.add(token)
                violations.append(token)

    return violations


# ペルソナの痕跡とみなす語尾・マーカー（部分文字列）
_PERSONA_TONE_MARKERS = (
    "んですよ", "ですよ", "ますよ", "ですね", "ますね", "んです",
    "ちゃい", "ちゃう", "ぴゃっ",
)

# 匿名掲示板の常体とみなす文末（文の終わりに現れた場合のみ）
_PLAIN_TONE_ENDING_RE = re.compile(
    r"(だからな|だろな|だよな|だなぁ|だなあ|だな|じゃね|だぜ)(?=[…‥。]*$)",
    re.MULTILINE,
)

# 無個性な説明文末
_BARE_POLITE_ENDING_RE = re.compile(r"(?:です|ます|でした|ました|ません|ませんでした|だ)[。．]\s*$")


def find_persona_tone_loss(
    body: str,
    *,
    first_person: str = "",
    exempt_max_chars: int = 20,
) -> str | None:
    """ペルソナ口調の喪失を検出する。違反があれば理由文字列、なければ None。

    条件A（ペルソナのマーカーが本文に1つも無い）を必須とし、さらに
    B1: 掲示板常体の文末がある、または
    B2: 3文以上すべてが「です／ます／だ＋。」で終わる無個性な説明口調
    のいずれかを満たす場合のみ違反とする（保守的に倒す）。
    引用行（> で始まる行）・コードブロック・URL は判定材料から除く。
    """
    raw = str(body or "").strip()
    if not raw:
        return None

    # 1. コードブロック除去 (```...```)
    cleaned = re.sub(r"```[\s\S]*?```", "", raw)
    # 2. インラインコード除去 (`...`)
    cleaned = re.sub(r"`[^`\n]+`", "", cleaned)
    # 3. URL除去
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    # 4. 引用行除去 (> で始まる行)
    non_quote_lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.strip().startswith(">")]
    target_text = "\n".join(non_quote_lines).strip()

    # 免除: 判定対象が空、または免除字数以下
    if not target_text or len(target_text) <= exempt_max_chars:
        return None

    # 条件A: ペルソナマーカー（語尾または一人称）が1つでもあるか
    has_marker = any(m in target_text for m in _PERSONA_TONE_MARKERS)
    if not has_marker and first_person and first_person.strip():
        has_marker = first_person.strip() in target_text

    if has_marker:
        return None

    # 条件B1: 掲示板常体文末の検出
    if _PLAIN_TONE_ENDING_RE.search(target_text):
        return "plain_endings_without_persona_marker"

    # 条件B2: 3文以上の無個性説明口調
    # ペルソナ特有の感嘆符や間（… や ！）が存在する場合は除外
    if "…" not in target_text and "！" not in target_text and "!" not in target_text:
        # 。、！、？、… または改行で文を分割（区切り記号を各文末尾に保持する肯定後読み）
        sentences = [s.strip() for s in re.split(r"(?<=[。．！？!?…\n])", target_text) if s.strip()]
        if len(sentences) >= 3 and all(_BARE_POLITE_ENDING_RE.search(s) for s in sentences):
            return "bare_explainer_tone"

    return None




