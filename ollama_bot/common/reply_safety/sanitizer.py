"""生成文の総合サニタイズおよびユーザー向け返答の抽出処理。"""
from __future__ import annotations

import re

from .cleaner import (
    _normalize_unintended_watashi,
    _strip_self_addressed_thanks,
    strip_markdown_artifacts,
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


def sanitize_generated_reply(text: str) -> str:
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
    t = _normalize_unintended_watashi(t)
    t = strip_trailing_reasoning_notes(t)
    return t.strip()


def extract_first_user_facing_reply(text: str) -> str:
    """サニタイズ後のテキストから、ラベル付き回答やユーザー向け本文を最優先で抽出し平文化する。"""
    t = sanitize_generated_reply(text)
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
    cleaned = strip_trailing_reasoning_notes(cleaned)
    
    return strip_markdown_artifacts(cleaned.strip())
