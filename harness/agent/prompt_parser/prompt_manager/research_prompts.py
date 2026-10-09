"""AIエージェント・ハーネス 外部調査プロンプト構築モジュール (research_prompts.py)

フェーズ1. コンテキストの構築（知覚・記憶の引き出し）において、
Web検索やブラウジング等で取得した外部調査結果（Verified Facts）を
推論用プロンプトへ統合・フォーマットする。
"""
from __future__ import annotations

import re
from typing import Any


def build_research_context_text(
    context_lines: list[str] | None = None,
    effective_user_text: str = "",
    original_user_text: str = "",
) -> str:
    """直近文脈とユーザー発言から調査用コンテキストテキストを構築する。"""
    parts: list[str] = []
    if context_lines:
        parts.append("\n".join(context_lines[-4:]))
    if effective_user_text and effective_user_text != original_user_text:
        parts.append(effective_user_text)
    return "\n".join(part for part in parts if part).strip()


def build_research_reply_base_prompt(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    topic_break: bool = False,
    last_assistant_text: str = "",
    intent_info: dict[str, Any] | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """外部調査結果を取り込むための基本プロンプトを構築する。"""
    payload = dict(intent_info or {})
    lead = "以下のメッセージは、直前の流れとは別話題です。" if topic_break else "以下のメッセージに、外部調査結果を踏まえて直接答えてください。"
    ref_match = re.match(r"^（([^（）\n]{1,120})への返信）[\s\n]*", user_text_for_prompt)
    parts = [lead]
    if ref_match:
        ref_info = ref_match.group(1).strip()
        clean_user_text = user_text_for_prompt[ref_match.end():].strip() or user_text_for_prompt
        parts.append(f"【返信元の相手発言】: {ref_info}")
        parts.append(f"対象メッセージ: {clean_user_text}")
        parts.append("※返信元の相手発言の話題に引きずられず、対象メッセージ（最新のユーザー発言）に直接答えてください。")
    else:
        parts.append(f"対象メッセージ: {user_text_for_prompt}")

    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{target_name}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウントID/ユーザー名ではなく必ず現在の対話相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
        "",
        *(["会話の続きに無理やり合わせなくてよいです。"] if topic_break else []),
        "今回の返答は、対象メッセージと外部調査結果を最優先してください。",
        "会話履歴、長期記憶、直前のAI発言に別の話題があっても、その話題へ戻らないでください。",
        "対象メッセージや外部調査結果に出てこない固有名詞・人物・作品名・ミーム・例えを勝手に持ち込まないでください。",
        "外部調査結果に含まれない断定は禁止です。",
    ])

    if last_assistant_text and any(ch in str(last_assistant_text or "") for ch in ("?", "？")):
        parts.extend([
            "",
            "直前のAI発言が質問だった場合だけ、その質問への返答として自然につながるように答えてください。",
            "ただし話題の中心は必ず対象メッセージと外部調査結果に置いてください。",
        ])

    if bool(payload.get("target_is_ai")) and str(payload.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}:
        parts.extend([
            "",
            "最新のユーザー発言はAIへの依頼または要求である可能性があります。",
            "依頼された内容に対して、頭ごなしな拒絶や放棄を避け、キャラクターの性格や口調を保ったまま自然に返答してください（セリフ指定や言葉遊び・気軽なリクエストの場合はある程度要望を叶えてあげてください）。",
        ])
    if bool(payload.get("should_clarify")):
        parts.extend([
            "",
            "意味が曖昧なら、決めつけずに短く確認してください。",
        ])
    if media_prompt_parts:
        parts.extend(media_prompt_parts)

    return "\n".join(parts).strip()
