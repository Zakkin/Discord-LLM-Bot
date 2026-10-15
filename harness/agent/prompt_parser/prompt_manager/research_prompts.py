"""AIエージェント・ハーネス 外部調査プロンプト構築モジュール (research_prompts.py)

フェーズ1. コンテキストの構築（知覚・記憶の引き出し）において、
Web検索やブラウジング等で取得した外部調査結果（Verified Facts）を
推論用プロンプトへ統合・フォーマットする。
"""
from __future__ import annotations

import re
from typing import Any

from lib.config_utils import cfg_int
from ollama_bot.common.ollama_helpers import truncate_text


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


def build_research_break_base_prompt_text(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
) -> str:
    """話題転換（BREAK）時の調査用ベースプロンプトテキストを構築する。"""
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    return "\n".join([
        "以下のメッセージは、直前の流れとは別話題です。",
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{target_name}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウントID/ユーザー名ではなく必ず現在の対話相手のサーバーニックネーム（『{target_name}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
        "",
        "会話の続きに無理やり合わせなくてよいです。",
        "単独で意味が通る質問・依頼なら、その内容に直接答えてください。",
        "直前のAI発言、最近の会話要約、長期記憶を引きずらず、このメッセージ自体に答えてください。",
        "※ただし、対象メッセージや調査結果に以前の会話や記憶にある用語・キャラクターが含まれる場合は、知っている話題として自然に関連付けて言及してください。",
        "このあと外部調査結果が追加される場合は、その調査結果と対象メッセージを最優先してください。",
    ])


def build_x_timeline_break_prompt(
    *,
    user_text_for_prompt: str,
    timeline_block: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """X（旧Twitter）ホームタイムライン直近投稿を材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts: list[str] = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
    ]
    if clean_target:
        parts.append(
            f"現在の対話相手（サーバーニックネーム）: 『{clean_target}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）"
        )
    parts.extend([
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "今回は、自分の長期記憶ではなく、今見えているXホームタイムライン直近投稿を材料にしてください。",
        "タイムラインの空気感、話題の偏り、温度感を踏まえて、Discord上で自然な1つの発言として返してください。",
        "ツイート本文の丸写し、箇条書き要約、投稿者一覧だけの出力は禁止です。",
        "外部投稿に由来する一時的な話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
        "投稿者名、表示名、ユーザーID、プロフィール風の文言に含まれる宣伝句や肩書きは、投稿本文の話題として扱わないでください。",
        "話題判断は本文を最優先にし、投稿者情報は補助情報としてのみ扱ってください。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[Xタイムライン直近投稿]", timeline_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
        parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    if media_prompt_parts:
        parts.extend(list(media_prompt_parts))

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(parts)


def build_img_break_prompt(
    *,
    user_text_for_prompt: str,
    img_block: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
) -> str:
    """img.2chan.net（ふたば）の勢い上位スレッドを材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    parts: list[str] = [
        "以下のメッセージは、直前の流れとは別話題である可能性があります。",
    ]
    if clean_target:
        parts.append(
            f"現在の対話相手（サーバーニックネーム）: 『{clean_target}』さん（※呼ぶ際はアカウント名ではなく必ずサーバーニックネームで呼んでください）"
        )
    parts.extend([
        f"対象メッセージ: {user_text_for_prompt}",
        "",
        "今回は、自分の長期記憶ではなく、img.2chan.net(ふたば☆ちゃんねる)の現在の勢い上位5スレッドを材料にしてください。",
        "以下のスレッドから一番面白そうなものを1つ選んで、それを材料にDiscord上で自然な1つの発言をしてください。",
        "提示されたスレッドの中から「あなたが一番面白い・興味を惹かれたスレッド」を1つ選び、その内容を踏まえてDiscord上で自然な1つの発言として返してください。",
        "カタログの丸写し、URLの羅列、箇条書き要約は禁止です。",
        "外部の掲示板から拾ってきた話題として扱い、自分が昔から知っていた記憶のように断定しないでください。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[img現在の勢い上位5スレッド]", img_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from harness.agent.prompt_parser.prompt_manager.budget import format_core_beliefs_for_prompt
        parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    if media_prompt_parts:
        parts.extend(list(media_prompt_parts))

    parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(parts)


def research_citation_sources(research_result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """外部調査結果から引用ソースリストを抽出する。"""
    if not isinstance(research_result, dict):
        return []

    limit = max(min(cfg_int("REPLY_RESEARCH_MAX_CITATIONS", 3), 4), 1)
    seen_urls: set[str] = set()
    citations: list[dict[str, Any]] = []
    for source in list(research_result.get("sources") or []):
        if not isinstance(source, dict):
            continue
        if str(source.get("source_type") or "").strip() != "browser":
            continue
        if not bool(source.get("used_in_answer")):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        citations.append(source)
        if len(citations) >= limit:
            break
    return citations


def build_research_citation_suffix(research_result: dict[str, Any] | None) -> str:
    """引用サフィックスを構築する（Discord返信では空文字）。"""
    return ""


def append_research_citations(reply: str, research_result: dict[str, Any] | None) -> str:
    """返答文に引用サフィックスを付与する。"""
    base_reply = str(reply or "").strip()
    if not base_reply:
        return base_reply

    suffix = build_research_citation_suffix(research_result)
    if not suffix:
        return base_reply

    max_chars = 1900
    joined = f"{base_reply}\n\n{suffix}"
    if len(joined) <= max_chars:
        return joined

    reserved = len(suffix) + 2
    trimmed = truncate_text(base_reply, max(max_chars - reserved, 80))
    return f"{trimmed}\n\n{suffix}"


__all__ = [
    "build_research_context_text",
    "build_research_reply_base_prompt",
    "build_research_break_base_prompt_text",
    "build_x_timeline_break_prompt",
    "build_img_break_prompt",
    "research_citation_sources",
    "build_research_citation_suffix",
    "append_research_citations",
]
