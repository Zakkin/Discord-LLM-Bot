"""話題転換（BREAK）プロンプト構築モジュール。"""
from __future__ import annotations

from collections.abc import Sequence


def build_memory_break_prompt(
    *,
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    channel_summary: str | None = None,
    memory_lines: list[str] | None = None,
    last_assistant_text: str = "",
    target_is_ai: bool = False,
    should_clarify: bool = False,
    mentioned_parts: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
    context_lines: Sequence[str] | None = None,
    topic_origin: str = "",
) -> str:
    """記憶（長期記憶・直前文脈）を材料にした話題転換（BREAK）プロンプトを構築する。"""
    clean_target = str(target_name or "").strip() or "ユーザー"
    bot_mention_str = f"<@{bot_user_id}>" if bot_user_id else ""
    from ..system_prompts import build_break_prompt_parts

    break_parts = build_break_prompt_parts(
        user_text_for_prompt,
        reply_style_guard=reply_style_guard,
        extra_reply_rule=extra_reply_rule,
        channel_summary=channel_summary,
        memory_lines=memory_lines,
        last_assistant_text=last_assistant_text,
        target_is_ai=target_is_ai,
        should_clarify=should_clarify,
        target_name=clean_target,
        context_lines=context_lines,
        topic_origin=topic_origin,
    )
    if mentioned_parts:
        insert_idx = 2
        if len(break_parts) >= insert_idx:
            break_parts = break_parts[:insert_idx] + ["", *mentioned_parts] + break_parts[insert_idx:]
        else:
            break_parts += ["", *mentioned_parts]

    if habit_profile_lines:
        break_parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        break_parts.extend([f"- {line}" for line in habit_profile_lines])

    if media_prompt_parts:
        break_parts.extend(list(media_prompt_parts))

    if core_beliefs:
        from ..budget import format_core_beliefs_for_prompt

        break_parts.extend(
            format_core_beliefs_for_prompt(
                core_beliefs,
                current_user_name=clean_target,
                aliases=target_aliases,
                target_user_id=target_uid,
            )
        )

    break_parts.extend([
        "",
        "【現在の対話相手】",
        f"サーバーニックネーム（表示名）: 『{clean_target}』さん",
        "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
        f"※相手を呼ぶ・お礼を言う場合は、アカウント名やユーザーIDではなく必ず現在の対話相手のサーバーニックネーム（『{clean_target}』さん等）を使ってください。あなた自身の名前（{bot_primary_name}等）や自分へのメンション（{bot_mention_str}等）で相手を呼ぶことは絶対に禁止です。",
        f"※ユーザーが発言内であなた（{bot_primary_name}）を呼んだりメンションした場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。返答文中にあなた自身へのメンションを含めないでください。",
    ])
    return "\n".join(break_parts)


def build_user_interest_break_prompt(
    *,
    user_text_for_prompt: str,
    interest_block: str,
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
    """サーバーメンバーの関心事を材料にした話題転換（BREAK）プロンプトを構築する。"""
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
        "今回は、直近の会話の流れや一般的な雑談ではなく、サーバーメンバーの関心事を材料にして話題を返してください。",
        "特定のユーザーに自然に尋ねてもよいし（例: 『そういえば〇〇さん、〜〜はどうなった？』）、チャンネル全体に向けてその話題を投げかけてもよい。",
        "データベースや分析結果を機械的に読み上げるような発言は禁止です。",
        "話題の丸写しや箇条書きは禁止です。",
    ])
    if reply_style_guard:
        parts.insert(1, reply_style_guard)
    if extra_reply_rule:
        parts.extend(["", extra_reply_rule])
    parts.extend(["", "[サーバーメンバーの関心事]", interest_block])

    if habit_profile_lines:
        parts.extend([
            "",
            "相手ごとの会話習慣プロファイル:",
            "※軽い傾向として反映し、最新の発言内容を優先してください。",
        ])
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if core_beliefs:
        from ..budget import format_core_beliefs_for_prompt

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


def build_break_reply_prompt(
    *,
    source: str = "memory",
    user_text_for_prompt: str,
    target_name: str = "ユーザー",
    bot_primary_name: str = "AI",
    bot_user_id: int | None = None,
    source_block: str = "",
    reply_style_guard: str = "",
    extra_reply_rule: str = "",
    channel_summary: str | None = None,
    memory_lines: list[str] | None = None,
    last_assistant_text: str = "",
    target_is_ai: bool = False,
    should_clarify: bool = False,
    mentioned_parts: list[str] | None = None,
    habit_profile_lines: list[str] | None = None,
    core_beliefs: list[str] | None = None,
    target_aliases: list[str] | None = None,
    target_uid: int | None = None,
    media_prompt_parts: list[str] | None = None,
    context_lines: Sequence[str] | None = None,
    topic_origin: str = "",
) -> str:
    """話題転換（BREAK）プロンプトをソース種別（memory, x, img, user_interest）に応じて統一構築する。"""
    src = str(source or "memory").strip().lower()
    if src == "x":
        from ..research_prompts import build_x_timeline_break_prompt

        return build_x_timeline_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            timeline_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    if src == "img":
        from ..research_prompts import build_img_break_prompt

        return build_img_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            img_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    if src == "user_interest":
        return build_user_interest_break_prompt(
            user_text_for_prompt=user_text_for_prompt,
            interest_block=source_block,
            target_name=target_name,
            bot_primary_name=bot_primary_name,
            bot_user_id=bot_user_id,
            reply_style_guard=reply_style_guard,
            extra_reply_rule=extra_reply_rule,
            habit_profile_lines=habit_profile_lines,
            core_beliefs=core_beliefs,
            target_aliases=target_aliases,
            target_uid=target_uid,
            media_prompt_parts=media_prompt_parts,
        )
    return build_memory_break_prompt(
        user_text_for_prompt=user_text_for_prompt,
        target_name=target_name,
        bot_primary_name=bot_primary_name,
        bot_user_id=bot_user_id,
        reply_style_guard=reply_style_guard,
        extra_reply_rule=extra_reply_rule,
        channel_summary=channel_summary,
        memory_lines=memory_lines,
        last_assistant_text=last_assistant_text,
        target_is_ai=target_is_ai,
        should_clarify=should_clarify,
        mentioned_parts=mentioned_parts,
        habit_profile_lines=habit_profile_lines,
        core_beliefs=core_beliefs,
        target_aliases=target_aliases,
        target_uid=target_uid,
        media_prompt_parts=media_prompt_parts,
        context_lines=context_lines,
        topic_origin=topic_origin,
    )
