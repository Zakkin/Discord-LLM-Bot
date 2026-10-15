from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("harness.agent.prompt_parser.parser.sanitizer")

from .cleaner import (
    _normalize_unintended_watashi,
    _strip_self_addressed_thanks,
    sanitize_false_style_accusation,
    sanitize_partner_misaddressing,
    sanitize_unrelated_media_hallucination,
    strip_bot_self_mentions,
    strip_hanging_tail_echo,
    strip_markdown_artifacts,
    strip_mimicked_kaomoji,
    strip_trailing_reasoning_notes,
)
from .control import (
    _extract_labeled_user_facing_line,
    _looks_like_control_line,
    _strip_leading_control_prefixes,
    _strip_leading_non_user_facing_paragraph,
    _strip_leading_style_tag_outline,
)
from .salvage import _strip_think_blocks


def sanitize_generated_reply(
    text: str,
    *,
    bot_user_id: int | None = None,
    replacement_name: str = "",
    target_name: str = "",
    other_names: list[str] | set[str] | None = None,
    user_text: str = "",
) -> str:
    """生成文から思考タグ、コードブロック、非公開前置き、メタ制御行などを除去してサニタイズする。"""
    t = str(text or "")
    t = _strip_think_blocks(t)
    t = re.sub(r"```(?:json|text)?\s*", "", t, flags=re.IGNORECASE)
    t = t.replace("```", "")
    t = re.sub(r"^\s*(assistant|ai)\s*[:：]\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _strip_leading_style_tag_outline(t)
    t = _strip_leading_non_user_facing_paragraph(t)
    t = _strip_leading_control_prefixes(t)
    t = _strip_self_addressed_thanks(t)
    eff_target = target_name or replacement_name
    t = strip_bot_self_mentions(t, bot_user_id=bot_user_id, replacement_name=eff_target)

    # other_names のセット化と user_text からの自律救出（多重防御）
    eff_other_names: set[str] = set()
    if other_names:
        for n in other_names:
            sn = str(n).strip()
            if sn:
                eff_other_names.add(sn)

    if user_text:
        m_ref = re.search(r"^[（\(]([^:：\n\(\)（）「」『』]{1,30}?)(?:の[「『]|への返信)", user_text.strip())
        if m_ref:
            ref_author = m_ref.group(1).strip()
            if ref_author and ref_author != eff_target:
                eff_other_names.add(ref_author)

    if eff_other_names:
        t = sanitize_partner_misaddressing(t, target_name=eff_target, other_names=eff_other_names)

    t = _normalize_unintended_watashi(t)
    t = strip_trailing_reasoning_notes(t)
    if user_text:
        t = strip_mimicked_kaomoji(user_text, t)
        t = sanitize_unrelated_media_hallucination(t, user_text=user_text)
        t = sanitize_false_style_accusation(t, user_text=user_text)
    t = strip_hanging_tail_echo(t, user_text=user_text)
    return t.strip()


def extract_first_user_facing_reply(text: str, *, user_text: str = "") -> str:
    """サニタイズ後のテキストから、ラベル付き回答やユーザー向け本文を最優先で抽出し平文化する。"""
    t = sanitize_generated_reply(text, user_text=user_text)
    if not t:
        return ""

    for sep in (
        "\n\nユーザー向け",
        "\n\n回答",
        "\n\n返答",
        "\n\n【返答】",
        "\n\n【回答】",
        "\n\n[返答]",
        "\n\n[回答]",
        "\n回答：",
        "\n返答：",
        "\n[返答]",
        "\n[回答]",
        "\n【返答】",
        "\n【回答】",
    ):
        if sep in "\n" + t:
            t = ("\n" + t).split(sep, 1)[-1].strip(" ：:\n")

    lines = [line.rstrip() for line in t.splitlines()]
    filtered: list[str] = []
    in_skip_block = False

    for line in lines:
        s = line.strip()
        if not s:
            in_skip_block = False
            if filtered and filtered[-1] != "":
                filtered.append("")
            continue
        labeled = _extract_labeled_user_facing_line(s)
        if labeled == "!SKIP!":
            in_skip_block = True
            continue
        if labeled is not None:
            s = labeled
            in_skip_block = False
            if not s:
                continue
        if re.match(r"^(思考|推論|reasoning|analysis|thinking process)[:：]", s, flags=re.IGNORECASE):
            in_skip_block = True
            continue
        if re.match(r"^(user|assistant|system)[:：]", s, flags=re.IGNORECASE):
            continue
        if in_skip_block:
            continue
        s = _strip_leading_control_prefixes(s)
        if not s or _looks_like_control_line(s):
            continue
        filtered.append(s)

    while filtered and filtered[-1] == "":
        filtered.pop()
    cleaned = _strip_leading_non_user_facing_paragraph("\n".join(filtered).strip())
    
    # 特殊なゴミの除去（「1. 」などの箇条書き番号が冒頭にある場合など）
    cleaned = re.sub(r"^\d+[\.、]\s*", "", cleaned)
    
    # Qwen 3.6がたまに出力する内省メモの残骸を除去
    cleaned = re.sub(r"^\[返答前の内省メモ\].*?(\n\n|\Z)", "", cleaned, flags=re.DOTALL)
    cleaned = _strip_leading_control_prefixes(cleaned)
    res = strip_markdown_artifacts(cleaned.strip())
    log.debug(
        "[prompt-manager] extract_first_user_facing_reply: input_len=%d -> output_len=%d",
        len(text or ""),
        len(res),
    )
    return res


def sanitize_unknown_denial_for_known_subject(reply: str) -> str:
    """既知のトピックやサーバーメンバーに対する不知・拒否・言いがかり表現を自然な驚き・戸惑いに置換する。"""
    cleaned = str(reply or "").strip()
    if not cleaned:
        return ""

    denial_patterns = [
        # 「〇〇」って何ですかそれ…！ / 『〇〇』とは何ですか？
        r"(?:「[^」]+」|『[^』]+』)?[、，\s]*[^\n！？。]*?(?:って何ですか(?:それ)?|とは何ですか|って誰ですか)[！？。…\s]*",
        # その名前/そんな用語/そんな名前/そんな言葉/その人/誰のこと/何のこと ... 全然知らない/わからない
        r"(?:その名前|そんな名前|そんな言葉|そんな用語|その人|誰のこと|何のこと)[、，\s]*[^\n！？。]*?(?:さっぱり|よく|まったく|全然|何一つ)?(?:わからない|分からな|知らな|わから|分から|知ら)[^\n！？。]*[！？。…\s]*",
        # コイト、そんな用語全然知らないんですよ…！ / そんな用語は知りません
        r"(?:[^\n！？。]{1,10}[、，\s]*)?そんな(?:用語|名前|言葉|人|こと)[^\n！？。]*?(?:全然|さっぱり|まったく)?(?:知らな|分からな|わからな)[^\n！？。]*[！？。…\s]*",
        # 突然変な名前出して困らせるじゃないですか…！ / 突然何の話？！
        r"(?:突然|急に)?(?:変な名前|そんなこと|そんな用語|そんな名前|何の話)[^\n！？。]*?(?:困らせる|言われても|出されても|何の話)[^\n！？。]*[！？。…\s]*",
    ]

    replaced_any = False
    for pat in denial_patterns:
        if re.search(pat, cleaned):
            cleaned = re.sub(pat, " ", cleaned)
            replaced_any = True

    if not replaced_any:
        return reply

    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()

    base_prefix = ""
    for ex in ("ぴゃっ…！？", "ぴゃっ…！", "えっと…", "あ、えっと…", "ひゃあ…！", "わわ…！", "えっ…！", "えっ？！"):
        if cleaned.startswith(ex):
            base_prefix = ex + " "
            cleaned = cleaned[len(ex):].strip()
            break

    if not cleaned or len(cleaned) < 5:
        return f"{base_prefix}急にその名前を聞かれてびっくりしちゃったんですけど…！".strip()

    if not any(token in cleaned for token in ("びっくり", "驚い", "急に")):
        return f"{base_prefix}急にその名前を聞かれてびっくりしちゃったんですけど…！ {cleaned}".strip()

    return f"{base_prefix}{cleaned}".strip()


_PLACEHOLDER_SUBSTRINGS: tuple[str, ...] = (
    "ここに",
    "thought",
    "所感",
    "一言コメント",
    "本音の一言",
    "格納",
    "文字列",
    "テキストを入力",
    "出力する内容",
    "出力内容",
    "プレースホルダー",
    "placeholder",
    "記述して",
    "入力して",
    "記載して",
    "記入して",
    "テンプレート",
    "キー名",
    "文字列として出力",
    "値を文字列として",
    "文字列として",
    "値を出力",
    "を踏まえ",
    "を考慮し",
    "を反映し",
    "を出力する",
    "一言を出力",
    "前向きな一言",
    "距離感を縮めようとする",
    "性格・口調",
    "関係性パラメータ",
    "パラメータ",
    "受け止めメモ",
    "私の価値観",
    "ツイートへの反応",
    "出来事の評価",
    "心の声",
    "（感情:",
    "(感情:",
    "名前を聞かれ",
    "名前を出され",
    "名前を言われ",
    "急に名前",
    "突然名前",
    "誰のことか",
    "意図が掴めない",
    "意図がつかめない",
    "意図がわからない",
)


def _has_japanese_char(text: str) -> bool:
    """日本語文字（ひらがな、カタカナ、漢字）が含まれているかを判定する。"""
    return bool(re.search(r"[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", text))


def looks_like_placeholder_thought(text: str | None) -> bool:
    """生成された所感がプロンプトの例文・プレースホルダー・メタ指示文であるかを判定する。"""
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if len(cleaned) < 5:
        return True
    if not _has_japanese_char(cleaned):
        return True

    lower = cleaned.lower()
    for sub in _PLACEHOLDER_SUBSTRINGS:
        if sub in lower:
            return True

    if re.search(r"ここに", cleaned):
        return True
    if re.search(r"(格納|出力|記述|入力|記載|記入)した?(文字列|テキスト|内容|もの)?", cleaned):
        return True
    if re.search(r"(?:踏まえ|考慮し|反映し|意識し).*(?:出力|記述|生成|作成)", cleaned):
        return True
    if re.search(r"(?:出力|記述|生成|作成)する[。！\s]*\Z", cleaned):
        return True
    if re.search(r"[（(]感情:\s*[a-zA-Z_]+[）)]", cleaned):
        return True
    if re.search(r"(?:名前|誰か).*?(?:聞かれ|出され|言われ|呼ばれ).*?(?:戸惑|わから|掴め|つかめ|困惑|意図)", cleaned):
        return True

    return False


def looks_like_copied_log_appraisal(text: str, logs: list[dict[str, Any]]) -> bool:
    """生成されたテキストが過去の対話ログの appraisal（受け止めメモ）の丸写しであるかを判定する。"""
    if not text or not logs:
        return False
    cleaned = text.strip()
    for item in logs:
        appraisal = str(item.get("bot_appraisal", "") or "").strip()
        if not appraisal:
            continue
        appraisal_clean = re.sub(r"[（(]感情:\s*[a-zA-Z_]+[）)]", "", appraisal).strip()
        if not appraisal_clean:
            continue
        if cleaned == appraisal_clean or appraisal_clean in cleaned or cleaned in appraisal_clean:
            return True
        if len(cleaned) >= 15 and len(appraisal_clean) >= 15:
            if cleaned[:15] in appraisal_clean or appraisal_clean[:15] in cleaned:
                return True
    return False


def sanitize_abnormal_rejection(reply: str, *, user_text: str = "") -> str:
    """相手のボケに対する『非常識なこと言わないで』『怖いですから』等のヒステリックな拒絶を等身大のツッコミにサニタイズする。"""
    cleaned = str(reply or "").strip()
    if not cleaned:
        return ""

    from .validation import ABNORMAL_REJECTION_PATTERNS
    if not any(re.search(pat, cleaned) for pat in ABNORMAL_REJECTION_PATTERNS):
        return reply

    t = cleaned
    # 「非常識なこと言わないでください」等
    t = re.sub(
        r"非常識(?:なこと|な人|扱い)?[、，\s]*(?:言わないで|言わんで|ですよ|です|ですね)[^！？。…\n]*[！？。…\s]*",
        "危ないですからやめてくださいよ…！ ",
        t,
    )
    # 「怖いですから言わないでください」等
    t = re.sub(
        r"怖い(?:ですから|だから|ですし)[、，\s]*(?:もう)?(?:言わないで|言わんで|やめて|話しかけないで)[^！？。…\n]*[！？。…\s]*",
        "落としちゃいますからちゃんと座ってくださいよ…！ ",
        t,
    )
    t = re.sub(
        r"(?:言わないで|言わんで|やめて)(?:ください|よ)?[、，\s]*(?:もう)?怖い(?:ですから|だから|ですし)[^！？。…\n]*[！？。…\s]*",
        "危ないですからやめてくださいよ…！ ",
        t,
    )
    t = re.sub(
        r"(?:頭(?:が|は)?(?:おかしい|変)|異常(?:ですよ|です)|気(?:は|でも)?確か)[^！？。…\n]*[！？。…\s]*",
        "な、何言ってるんですか…！ ",
        t,
    )
    t = re.sub(
        r"[^\n！？。…]{0,35}(?:言っ|話して)る意味(?:を)?勘違いして(?:ない|ません|る)[^！？。…\n]*[！？。…\s]*",
        "そうなんですよ…！ ",
        t,
    )
    t = re.sub(
        r"[^\n！？。…]{0,35}(?:勘違い|誤解)(?:してないですか|してませんか|しないでください)[^！？。…\n]*[！？。…\s]*",
        "そうなんですよ…！ ",
        t,
    )
    return re.sub(r"[ \t]+", " ", t).strip()

