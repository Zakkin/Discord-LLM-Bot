"""制御文、内省メモ、指示文行、スタイルアウトライン等の検出と除去処理。"""
from __future__ import annotations

import re

from .cleaner import _strip_leading_list_marker, strip_markdown_artifacts
from .patterns import looks_like_prompt_leak


def _looks_like_analysis_label(label: str) -> bool:
    normalized = re.sub(r"\s+", "", str(label or "").strip())
    if not normalized:
        return False
    normalized = normalized.strip("【】[]（）()")
    if not normalized or len(normalized) > 64:
        return False
    exact_labels = {
        "本題",
        "要点",
        "結論",
        "狙い",
        "方針",
        "返答",
        "回答",
        "最終返答",
        "最終回答",
        "直接的な回答",
        "状況整理",
        "文脈整理",
        "話題整理",
    }
    if normalized in exact_labels:
        return True
    label_markers = (
        "受け止め方",
        "直接的な回答",
        "返答方針",
        "回答方針",
        "返答の方向",
        "回答の方向",
        "返答の狙い",
        "切り抜ける方向",
        "言及",
        "本題",
        "話題",
        "文脈",
        "状況",
        "表現",
        "相手",
        "ユーザー",
        "発言",
        "意図",
        "感情",
        "関係性",
        "警戒",
        "距離感",
        "態度",
        "トーン",
        "リアクション",
        "分析",
        "判断",
        "注意",
        "禁止",
        "避ける",
        "口調",
        "調整",
        "戦略",
    )
    return any(marker in normalized for marker in label_markers)


def _looks_like_labeled_analysis_line(line: str, *, allow_generic_bullet: bool = False) -> bool:
    s = str(line or "").strip()
    if not s:
        return False
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s).strip()
    s = re.sub(r"^\s{0,3}>\s?", "", s).strip()
    s = re.sub(r"^(`+|[*_~]{1,2})(.*?)(\1)$", r"\2", s).strip()
    without_marker, bullet_like = _strip_leading_list_marker(s)
    match = re.match(r"^([^:：\n]{1,72})[:：]\s*(.*)$", without_marker)
    if not match:
        return False
    label = str(match.group(1) or "").strip()
    body = str(match.group(2) or "").strip()
    if _looks_like_analysis_label(label):
        return True
    # A compact bullet-list key/value line before a final answer is usually an outline,
    # even when the model invents a one-off label that our marker list has not seen yet.
    if allow_generic_bullet and bullet_like and body and len(label) <= 48:
        return bool(re.search(r"[ぁ-んァ-ヶ一-龠]", label)) and bool(re.search(r"[ぁ-んァ-ヶ一-龠]", body))
    return False


def _looks_like_structured_analysis_block(text: str) -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    labeled_count = sum(1 for line in lines if _looks_like_labeled_analysis_line(line, allow_generic_bullet=True))
    if labeled_count >= 2:
        return True
    if labeled_count == len(lines) and labeled_count >= 1:
        return True
    return False


def _looks_like_instruction_leak_line(line: str) -> bool:
    s = str(line or "").strip()
    if not s:
        return False
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
    s = re.sub(r"^\s{0,3}>\s?", "", s)
    s = re.sub(r"^(?:[-*・]|[0-9０-９]+[.)．、])\s*", "", s).strip()
    s = s.strip("「」『』\"'")
    if not s:
        return False

    has_control_subject = bool(
        re.search(
            r"(?:AI|bot|ボット|<think>|内省|思考|推論|Markdown|箇条書き|指示文|制御文|プロンプト)",
            s,
            flags=re.IGNORECASE,
        )
    )
    if not has_control_subject:
        return False

    has_imperative = bool(
        re.search(r"(?:絶対に|必ず|禁止)", s)
        or re.search(r"(?:しない|使わない|出力しない|書かない|含めない|言わない)こと(?:[。.!！]|$)", s)
    )
    has_output_verb = bool(re.search(r"(?:使わない|出さない|出力しない|書かない|含めない|言わない|削除|無視|禁止)", s))
    return has_imperative and has_output_verb


def _looks_like_control_line(line: str) -> bool:
    s = str(line or "").strip()
    if not s:
        return False

    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
    s = re.sub(r"^\s{0,3}>\s?", "", s)
    s = re.sub(r"^(`+|[*_~]{1,2})(.*?)(\1)$", r"\2", s).strip()
    bullet_like = bool(re.match(r"^(?:[-*・]|[0-9０-９]+[.)．、])\s*", s))
    normalized = re.sub(r"^(?:[-*・]|[0-9０-９]+[.)．、])\s*", "", s).strip()
    normalized = re.sub(r"^(`+|[*_~]{1,2})(.*?)(\1)$", r"\2", normalized).strip()
    if not normalized:
        return False
    if _looks_like_instruction_leak_line(normalized):
        return True
    if _looks_like_labeled_analysis_line(s):
        return True

    if re.search(r"^(?:この)?ルール(?:は|を|が)?(?:絶対|厳守|遵守|守る)", normalized):
        return True
    if re.search(r"^(?:指示|条件|設定)(?:を|に)?(?:遵守|厳守|従い|従って|守って)", normalized):
        return True
    if re.search(r"^(?:了解|承知|了解いた|承知いた)(?:しました|致しました|した)[。！!]?$", normalized):
        return True

    control_markers = (
        "キャラクター設定",
        "口調を維持",
        "返答本文",
        "出力",
        "制御文",
        "指示文",
        "外部調査",
        "調査結果",
        "参照した候補ソース",
        "最終優先指示",
        "対象メッセージ",
        "会話履歴",
        "直前のAI発言",
        "返答前の内省メモ",
        "内省メモ",
        "感情の軸",
        "返答方針",
        "避けること",
        "熟考トリガー",
        "これらを踏まえて",
        "内部整理用",
        "観測した投稿本文",
        "本文に現れた話題",
        "タイムラインの傾向",
        "今観測した投稿",
        "取れた投稿",
        "ただ列挙",
        "全体の傾向",
        "複数投稿の共通点",
        "目立った個別例",
        "直近で気になっている題材",
        "最近気になっている題材",
        "内部素材",
        "返答の温度感",
        "相手への態度",
        "返答の長さ",
        "相手の発言に対するリアクション",
        "リアクション",
        "感情表現の指針",
        "感情",
        "現在の感情",
        "感情状態",
        "関係性",
        "親密度",
        "信頼度",
        "警戒心",
        "冷淡さ",
        "相手の発言分析",
        "ユーザー発言分析",
        "発言分析",
        "何に対してどう返すか",
        "何かを教える必要があるか",
        "相手の言葉の裏の意味",
        "本題",
        "要点",
        "結論",
        "受け止め方",
        "直接的な回答",
        "返答の方向",
        "回答の方向",
        "切り抜ける方向",
        "返答戦略",
        "感情調整",
        "口調調整",
        "禁止事項",
        "オウム返し",
        "前の話題",
        "action_style",
        "2〜5文",
        "220〜500文字",
        "してください",
        "してはいけません",
        "禁止です",
        "このルール",
        "絶対厳守",
        "厳守",
        "遵守",
        "直近投稿の空気感",
        "話題の偏り",
        "温度感を踏まえて",
        "Discordの流れを壊さない",
        "話題振りを一つだけ",
        "自然に出せ",
        "一時的な話題として扱い",
        "ツイート本文の丸写し",
        "本文の丸写し",
        "各スレッドの列挙",
        "宣伝句や肩書き",
        "話題判断は本文を最優先",
        "サーバーメンバーの関心事",
        "ユーザー関心事",
        "返答本文だけ",
        "<think>",
    )
    if normalized in {"返答", "回答", "最終返答", "最終回答", "ユーザー向け返答", "ユーザー向け回答"}:
        return True

    if normalized.endswith(("出せ。", "出せ", "出せ！", "出せ!", "出せ…！", "出せ...!", "すること。", "すること", "禁止。", "禁止")):
        if any(marker in normalized for marker in control_markers) or bullet_like:
            return True

    if re.match(
        r"^(?:相手の意図|ユーザーの最新発言|最新のユーザー発言|最新の相手の発言|会話の背景|返答スタイル|応答スタイル)\s*[:：]",
        normalized,
        flags=re.IGNORECASE,
    ):
        return True

    control_prefixes = (
        "返答前の内省メモ",
        "内省メモ",
        "感情の軸",
        "返答方針",
        "避けること",
        "熟考トリガー",
        "キャラ維持指示",
        "相手への態度",
        "返答の温度感",
        "返答の長さ",
        "相手の発言に対するリアクション",
        "感情表現の指針",
        "相手の発言分析",
        "発言分析",
        "何に対してどう返すか",
        "何かを教える必要があるか",
        "相手の言葉の裏の意味",
        "本題",
        "要点",
        "結論",
        "受け止め方",
        "直接的な回答",
        "返答の方向",
        "回答の方向",
        "切り抜ける方向",
        "直近で気になっている題材",
        "最近気になっている題材",
        "内部素材",
        "相手の意図",
        "ユーザーの最新発言",
        "最新のユーザー発言",
        "最新の相手の発言",
        "会話の背景",
        "返答スタイル",
        "応答スタイル",
        "ユーザー発言分析",
        "返答戦略",
        "感情調整",
        "口調調整",
        "※内省メモ",
        "※内部整理用",
        "※これらを踏まえて",
        "これらを踏まえて",
    )
    if re.match(rf"^(?:{'|'.join(map(re.escape, control_prefixes))})\s*[:：]", normalized):
        return True

    if any(marker in normalized for marker in control_markers) and (
        bullet_like
        or normalized.endswith(("してください。", "してください", "してはいけません。", "してはいけません", "禁止です。", "禁止です"))
    ):
        return True

    if re.match(r"^【[^】]*(?:外部調査|参照|指示|対象メッセージ|会話履歴|最終優先指示)[^】]*】$", normalized):
        return True
    if re.match(r"^【[^】]*(?:返答前の内省メモ|内省メモ|感情の軸|返答方針|避けること|熟考トリガー)[^】]*】$", normalized):
        return True
    if re.match(r"^\[[^\]]*(?:外部調査|指示|対象メッセージ|会話履歴|最終優先指示|内省メモ)[^\]]*\]$", normalized):
        return True
    return False


def _extract_labeled_user_facing_line(line: str) -> str | None:
    s = str(line or "").strip()
    if not s:
        return ""

    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)
    s = re.sub(r"^\s{0,3}>\s?", "", s)
    s = re.sub(r"^(`+|[*_~]{1,2})(.*?)(\1)$", r"\2", s).strip()

    skip_prefixes = (
        "返答前の内省メモ",
        "内省メモ",
        "感情の軸",
        "返答方針",
        "避けること",
        "熟考トリガー",
        "キャラ維持指示",
        "相手への態度",
        "返答の温度感",
        "返答の長さ",
        "相手の発言に対するリアクション",
        "感情表現の指針",
        "相手の発言分析",
        "発言分析",
        "相手の意図",
        "ユーザーの最新発言",
        "最新のユーザー発言",
        "最新の相手の発言",
        "会話の背景",
        "返答スタイル",
        "応答スタイル",
        "思考",
        "推論",
        "reasoning",
        "analysis",
        "thinking process",
        "本題",
        "要点",
        "結論",
        "受け止め方",
        "直接的な回答",
        "返答の方向",
        "回答の方向",
        "切り抜ける方向",
        "ユーザー発言分析",
        "返答戦略",
        "感情調整",
        "口調調整",
    )
    if re.match(rf"^(?:{'|'.join(map(re.escape, skip_prefixes))})\s*[:：]", s, flags=re.IGNORECASE):
        return "!SKIP!"

    reply_labels = (
        "返答",
        "回答",
        "最終返答",
        "最終回答",
        "ユーザー向け返答",
        "ユーザー向け回答",
        "final answer",
        "answer",
        "reply",
    )
    match = re.match(rf"^(?:{'|'.join(map(re.escape, reply_labels))})\s*[:：]\s*(.*)$", s, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip()

    bracket_match = re.match(
        rf"^[【\[]\s*(?:{'|'.join(map(re.escape, reply_labels))})\s*[】\]]\s*(.*)$",
        s,
        flags=re.IGNORECASE,
    )
    if bracket_match:
        return str(bracket_match.group(1) or "").strip()

    return None


def _strip_leading_control_prefixes(text: str) -> str:
    """行または全体の先頭にくっついている内部指示文・内省メモ区切り・メタ前置きなどを除去する。"""
    t = str(text or "").strip()
    if not t:
        return ""

    for _ in range(3):
        prev = t
        # [返答前の内省メモ] や [/返答前の内省メモ]
        t = re.sub(r"^[\[【]/?(?:返答前の)?内省メモ[\]】]\s*", "", t, flags=re.IGNORECASE)
        # ※内省メモはここまでです。これらを踏まえて、... などのプレフィックス
        t = re.sub(
            r"^※?\s*内省メモはここまでです[。、，\s]*"
            r"(?:これらを踏まえて[、，\s]*)?"
            r"(?:必ずキャラクターの言葉で直接返答を始めてください[。、，\s]*)?",
            "",
            t,
            flags=re.IGNORECASE,
        )
        # ※内部整理用。...
        t = re.sub(
            r"^※?\s*内部整理用[。、，\s]*"
            r"(?:以下の箇条書きや見出しは最終返答に出力せず[、，\s]*内容だけ自然に反映してください[。、，\s]*)?",
            "",
            t,
            flags=re.IGNORECASE,
        )
        # これらを踏まえて、
        t = re.sub(r"^(?:※?\s*これらを踏まえて[、，\s]*)+", "", t)
        # 思考プロセスや思考タグ（think）、前置き、解説は出力せず...
        t = re.sub(
            r"^※?\s*思考プロセスや(?:思考タグ(?:[（\(]think[）\)])?|<think>タグ|タグ)?[、，\s]*前置き[、，\s]*解説は出力せず[、，\s]*"
            r"(?:あなたのキャラクター(?:[（\(]Assistant[）\)])?としての自然な日本語の返答本文だけを(?:1〜2文で)?直接出力してください[。、，\s]*)?",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = re.sub(
            r"^※?\s*あなたのキャラクター(?:[（\(]Assistant[）\)])?としての自然な日本語の返答本文だけを(?:1〜2文で)?直接出力してください[。、，\s]*",
            "",
            t,
            flags=re.IGNORECASE,
        )
        t = t.strip()
        if t == prev:
            break

    return t


def _looks_like_style_tag_outline_before_reply(before: str, after: str) -> bool:
    if not _looks_like_conversational_reply_excerpt(after):
        return False

    lines = [line.strip() for line in str(before or "").splitlines() if line.strip()]
    if not lines or len(lines) > 4:
        return False

    first = lines[0].strip()
    if not re.fullmatch(r"[,，\s]*(?:[a-z][a-z0-9_-]*)(?:\s*,\s*[a-z][a-z0-9_-]*){1,6}\s*", first, flags=re.IGNORECASE):
        return False

    style_markers = (
        "short",
        "minimal",
        "minimal_explanation",
        "direct",
        "direct_reply",
        "reaction",
        "clarify",
        "validate",
        "tease",
        "answer",
    )
    normalized_first = first.lower().replace("-", "_")
    if not any(marker in normalized_first for marker in style_markers):
        return False

    if len(lines) == 1:
        return True
    return any(
        re.match(r"^(?:[-*+・]|[0-9０-９]+[.)．、])\s*", line)
        or re.search(r"(?:返答|回答|説明|話題|意味不明|距離を置く|正解|方針|意図|聞かれても)", line)
        for line in lines[1:]
    )


def _looks_like_meta_reply_preface(text: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 90:
        return False

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        return False

    normalized = strip_markdown_artifacts(lines[0]).strip()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.strip("「」『』\"'")
    if not normalized or len(normalized) > 60:
        return False

    return bool(
        re.fullmatch(
            r"(?:以下の|次の|この|こんな|その|上記の)?"
            r"(?:ような|ように|感じの|感じで)?"
            r"[^。！？!?]{0,24}"
            r"(?:返答|回答|返信|応答)"
            r"(?:にします|にいたします|します|いたします|をします|をいたします|"
            r"を返します|を返す|を作ります|を作る|を生成します|を生成する|"
            r"で返します|で返す|として返します|として返す)"
            r"[。.!！]*",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_conversational_reply_excerpt(text: str) -> bool:
    value = str(text or "").strip()
    if not value or not re.search(r"[ぁ-んァ-ヶ一-龠]", value):
        return False
    if re.search(r"[!?？！]", value):
        return True
    if len(value) >= 18:
        return True
    return bool(
        re.search(
            r"(?:だろ|だな|だよ|だ(?:[。…]|\s|$)|です|ます|じゃね|じゃない|ねえ|ねぇ|"
            r"しろ|して|する|した|してる|している|ない|いる|ある|余計|最初|触る|"
            r"話せ|黙ってろ|わかる|知らね|知らない|思う|聞く)(?:[。…]|\s|$)",
            value,
        )
    )


def _looks_like_fragmentary_meta_summary(text: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 90:
        return False

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines or len(lines) > 2:
        return False

    if re.search(r"[!?？！]", value):
        return False
    if re.search(r"(俺|おれ|私|わたし|僕|ぼく|お前|おまえ|あんた|君|きみ|てめえ)", value):
        return False
    if re.search(
        r"(?:だろ|だな|だよ|だ(?:[。…]|\s|$)|です|ます|でした|ません|じゃね|じゃない|"
        r"ねえ|ねぇ|しろ|して|する|した|してる|している|ない|いる|ある|余計|最初|"
        r"触る|話せ|黙ってろ|わかる|知らね|知らない|思う|聞く)(?:[。…]|\s|$)",
        value,
    ):
        return False

    chunks = [
        chunk.strip(" 　、,，。./／・")
        for chunk in re.split(r"[、,，/／・]+", value)
        if chunk.strip(" 　、,，。./／・")
    ]
    if not chunks:
        return False

    if re.search(r"(?:への|としての|についての|に対する|からの|での|上の|用の)", value):
        return len(chunks) >= 2

    if len(chunks) >= 3 and sum(len(chunk) for chunk in chunks) >= 12:
        return True

    return False


def _strip_leading_non_user_facing_paragraph(text: str) -> str:
    value = str(text or "").strip()
    while value:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", value) if part.strip()]
        if len(paragraphs) < 2:
            return value
        lead = paragraphs[0]
        remainder = "\n\n".join(paragraphs[1:]).strip()
        
        is_meta_lead = (
            _looks_like_meta_reply_preface(lead)
            or _looks_like_fragmentary_meta_summary(lead)
            or _looks_like_structured_analysis_block(lead)
            or _looks_like_control_line(lead)
            or looks_like_prompt_leak(lead)
        )
        if not is_meta_lead:
            return value
            
        if not _looks_like_conversational_reply_excerpt(remainder):
            return value
        value = remainder
    return value


def _strip_leading_style_tag_outline(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    lines = value.splitlines()
    first_nonempty_idx: int | None = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_nonempty_idx = idx
            break
    if first_nonempty_idx is None:
        return ""

    first = lines[first_nonempty_idx].strip()
    remainder = "\n".join(lines[first_nonempty_idx + 1:]).strip()
    if remainder and _looks_like_style_tag_outline_before_reply(first, remainder):
        return remainder
    return value
