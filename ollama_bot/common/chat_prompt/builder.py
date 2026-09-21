from __future__ import annotations

import logging
import re
from collections import deque
from typing import Any, Awaitable, Callable, Optional

import discord

from lib.text_utils import compact_whitespace

from ..config_helpers import cfg

from ..discord_helpers import (
    author_id,
    channel_id,
    collect_reply_chain,
    content,
    display_name,
    find_reply_parent_from_cache,
    format_message_reactions,
    prune_reply_chain_from_cache,
    recent_context_items,
)
from .. import memory_logic
from .. import prospective_memory
from .. import user_profile_logic
from ..working_memory import (
    build_working_context,
    format_working_context_for_prompt,
)
from ..bot_identity import BotIdentity, resolve_bot_identity
from ..memory_store import MemoryStore
from .. import ollama_helpers
from ...ollama_chat_.ollama_chat_types import EmotionState
from ...ollama_chat_.ollama_chat_texts import (
    USER_REPLY_BASE_PARTS,
    append_chat_reply_output_suffix,
    build_user_prompt_extra_rule_parts,
    build_user_prompt_target_is_ai_parts,
)
from .budget import (
    _build_recent_turns,
    _context_max_age_sec,
    _filter_prompt_context_lines,
    _get_relevant_episode_lines,
    _trim_prompt_inputs,
    build_agent_persona_parts,
)
from .analyzer import (
    _analyze_pre_reply_context,
    _looks_like_external_research_context_request,
    detect_third_party_hearsay,
)

log = logging.getLogger("ollama_bot.common.chat_prompt.builder")


def _format_reply_tree_context(
    reply_chain: list[discord.Message],
    *,
    bot_user_id: Optional[int] = None,
    cached_parent_turn: Optional[dict[str, Any]] = None,
) -> list[str]:
    lines: list[str] = []
    if cached_parent_turn:
        p_name = str(cached_parent_turn.get("name") or "ユーザー")
        p_content = str(cached_parent_turn.get("content") or "").strip()
        if p_content:
            p_content_clean = compact_whitespace(p_content)
            p_content_clean = ollama_helpers.truncate_text(p_content_clean, 100)
            lines.append(f"・『{p_name}』: 「{p_content_clean}」")

    for msg in reply_chain:
        aid = author_id(msg)
        is_bot = bot_user_id is not None and aid == bot_user_id
        m_name = "あなた（AI）" if is_bot else f"『{display_name(msg)}』"
        m_content = compact_whitespace(content(msg))
        if not m_content:
            continue
        m_content = ollama_helpers.truncate_text(m_content, 120)
        lines.append(f"・{m_name}: 「{m_content}」")
    return lines


def _detect_recent_bot_utterance_tsukkomi(
    user_text: str,
    recent_bot_utterances: list[str],
) -> tuple[bool, str, str]:
    """ユーザーの最新発言が、直近のBot自身の発言に対するツッコミ・疑問であるかを判定する。"""
    raw_user = str(user_text or "").strip()
    if not raw_user:
        return False, "", ""
    clean_user = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", raw_user, flags=re.DOTALL).strip()
    clean_user = re.sub(r"^[（(].*?への返信[）)]\s*", "", clean_user, flags=re.DOTALL).strip()
    # 意見・感想・質問の構文（〜についてどう思う等）は自発言へのツッコミではない
    if any(op in clean_user for op in ("どう思う", "どう考える", "どう？", "どうですか", "教えて", "教えろ")):
        return False, "", ""

    is_question_or_tsukkomi = any(q in clean_user for q in ("？", "?", "なの", "ですか", "だっけ", "言った", "言ってた", "誰のこと", "何言って", "どういうこと"))
    if not is_question_or_tsukkomi:
        return False, "", ""

    compact_user = re.sub(r"[？?！!。、\s]", "", clean_user)
    if not compact_user:
        return False, "", ""

    time_and_topic_keywords = [
        "朝", "昼", "夜", "深夜", "昨日", "今日", "明日", "今朝", "今夜",
        "起き", "寝", "叫", "誰のこと", "何言って", "何のこと", "誰それ", "何の話",
    ]
    user_keywords = [kw for kw in time_and_topic_keywords if kw in compact_user]
    if len(compact_user) <= 8:
        stem = compact_user.rstrip("なののかですがだよだね")
        if stem and len(stem) >= 2 and stem not in user_keywords:
            user_keywords.append(stem)

    for utterance in recent_bot_utterances:
        raw_u = str(utterance or "").strip()
        if not raw_u:
            continue
        for kw in user_keywords:
            if kw and len(kw) >= 1 and kw in raw_u:
                return True, raw_u, kw

    return False, "", ""



async def _build_user_prompt(
    message: discord.Message,
    cache: dict[int, deque[dict[str, Any]]],
    memory_store: MemoryStore,
    *,
    bot_user_id: Optional[int],
    emotion_state: EmotionState | None = None,
    core_beliefs: Optional[list[str]] = None,
    effective_user_text: str = "",
    media_prompt_parts: Optional[list[str]] = None,
    habit_profile_lines: Optional[list[str]] = None,
    fetch_recent_context_lines: Callable[[discord.Message], Awaitable[list[str]]],
    find_last_assistant_message_text: Callable[..., Awaitable[str]],
    preferred_emotion_tags: Optional[list[str]] = None,
    user_relationships: Optional[dict[int, Any]] = None,
    bot_identity: Optional[BotIdentity] = None,
) -> tuple[str, list[int], list[str], str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    cid = channel_id(message)
    cached_prompt_items = recent_context_items(
        cache,
        cid,
        before_message=message,
        max_age_sec=_context_max_age_sec(),
    ) if cid is not None else []
    context_lines = [str(item.get("line", "")) for item in cached_prompt_items if item.get("line")]
    handled_message_ids: list[int] = []

    if not context_lines:
        context_lines = await fetch_recent_context_lines(message)
    context_lines = _filter_prompt_context_lines(context_lines)

    parts = list(USER_REPLY_BASE_PARTS)
    reply_style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
    if reply_style_guard:
        parts.insert(2, reply_style_guard)

    last_assistant_text = await find_last_assistant_message_text(message, bot_user_id=bot_user_id, cache=cache)
    user_text_for_prompt = (effective_user_text or content(message) or "（本文なし）").strip()
    is_replying_to_old_message = "への返信）" in user_text_for_prompt
    is_reply_to_other_quick = is_replying_to_old_message and not ("あなたの" in user_text_for_prompt or "Assistant" in user_text_for_prompt)
    if is_reply_to_other_quick or is_replying_to_old_message:
        search_context_text = user_text_for_prompt
    else:
        search_context_text = f"{last_assistant_text} {user_text_for_prompt}".strip() if last_assistant_text else user_text_for_prompt

    is_hearsay, hearsay_subject, hearsay_quote = detect_third_party_hearsay(
        user_text_for_prompt,
        context_lines=context_lines,
    )

    message_guild = getattr(message, "guild", None)
    gid = getattr(message_guild, "id", None) if message_guild is not None else None

    raw_memory_lines, raw_channel_summary = await memory_logic.get_relevant_memories(
        cfg=cfg,
        store=memory_store,
        guild_id=gid,
        channel_id=cid,
        user_id=author_id(message),
        preferred_emotion_tags=preferred_emotion_tags,
        user_text=search_context_text,
    )
    habit_profile_lines = list(habit_profile_lines or [])
    raw_recent_bot_utterances = await memory_store.get_recent_bot_utterances(
        persona=memory_logic._persona_from_cfg(cfg),
        guild_id=gid,
        channel_id=cid,
        limit=3,
        max_age_sec=_context_max_age_sec(),
    )
    recent_bot_utterances: list[str] = []
    for raw_utterance in raw_recent_bot_utterances:
        cleaned_utterance = ollama_helpers.extract_first_user_facing_reply(str(raw_utterance or ""))
        if cleaned_utterance and not ollama_helpers.looks_like_abnormal_assistant_reply(cleaned_utterance):
            recent_bot_utterances.append(cleaned_utterance)
    external_research_request = _looks_like_external_research_context_request(user_text_for_prompt)
    recent_turns = _build_recent_turns({}, channel_id_value=None, context_lines=context_lines, limit=12)
    working_ctx = build_working_context(
        recent_turns,
        channel_summary=raw_channel_summary,
        emotion_state=emotion_state,
        last_bot_text=last_assistant_text,
    )
    pending_intents = await prospective_memory.match_cues(
        store=memory_store,
        persona=memory_logic._persona_from_cfg(cfg),
        guild_id=gid,
        channel_id=cid,
        user_id=author_id(message),
        text=user_text_for_prompt,
    )
    working_ctx["future_prompts"] = pending_intents
    episode_lines = await _get_relevant_episode_lines(
        memory_store=memory_store,
        guild_id=gid,
        channel_id_value=cid,
        user_id=author_id(message),
        current_user_text=user_text_for_prompt,
        working_ctx=working_ctx,
    )
    reply_chain = await collect_reply_chain(message)
    if cid is not None and reply_chain:
        reply_chain = prune_reply_chain_from_cache(cache, cid, reply_chain)
    latest_ref = reply_chain[-1] if reply_chain else None
    ref_author_id_val = author_id(latest_ref) if latest_ref else None
    is_reply_to_bot = bool(latest_ref is not None and bot_user_id is not None and ref_author_id_val == bot_user_id)
    is_reply_to_other_user = bool(latest_ref is not None and not is_reply_to_bot)
    if not latest_ref and "への返信）" in user_text_for_prompt:
        if "あなたの" in user_text_for_prompt or "Assistant" in user_text_for_prompt:
            is_reply_to_bot = True
        else:
            is_reply_to_other_user = True

    cached_reply_parent: Optional[dict[str, Any]] = None
    if is_reply_to_bot and latest_ref is not None and len(reply_chain) <= 1:
        ref_mid = getattr(latest_ref, "id", None)
        cached_reply_parent = find_reply_parent_from_cache(
            cache,
            cid,
            ref_mid if isinstance(ref_mid, int) else None,
            bot_user_id=bot_user_id,
        )

    reply_context_hint: Optional[str] = None
    if reply_chain and latest_ref is not None:
        tree_summary_lines = _format_reply_tree_context(
            reply_chain,
            bot_user_id=bot_user_id,
            cached_parent_turn=cached_reply_parent,
        )
        if len(tree_summary_lines) >= 2:
            parent_summaries = tree_summary_lines[:-1]
            reply_context_hint = " -> ".join(s.lstrip("・") for s in parent_summaries)

    if bot_identity is None:
        bot_identity = resolve_bot_identity(bot=None, message=message)

    # 会話中で言及されたサーバーメンバーの事前解決
    author_name_val = getattr(getattr(message, "author", None), "name", "")
    author_disp_val = getattr(getattr(message, "author", None), "display_name", author_name_val)
    exclude_names = [author_disp_val, author_name_val] if author_disp_val or author_name_val else None

    resolved_mentioned_users, mentioned_parts = await user_profile_logic.resolve_and_format_mentioned_users(
        user_text=user_text_for_prompt,
        context_lines=context_lines,
        guild_id=gid,
        exclude_user_id=author_id(message),
        exclude_names=exclude_names,
        memory_store=memory_store,
        user_relationships=user_relationships,
        cfg_fn=cfg,
        current_user_name=author_disp_val,
    )

    intent_info, memory_lines, channel_summary, deliberation_info = await _analyze_pre_reply_context(
        current_user_text=user_text_for_prompt,
        last_assistant_text=last_assistant_text,
        memory_lines=raw_memory_lines,
        channel_summary=raw_channel_summary,
        preferred_emotion_tags=preferred_emotion_tags,
        is_reply_to_other_user=is_reply_to_other_user,
        reply_context_hint=reply_context_hint,
        recent_bot_utterances=recent_bot_utterances,
        bot_identity=bot_identity,
        mentioned_users=resolved_mentioned_users,
    )
    is_self_tsukkomi = False
    matched_bot_utterance = ""
    matched_kw = ""
    if not resolved_mentioned_users:
        is_self_tsukkomi, matched_bot_utterance, matched_kw = _detect_recent_bot_utterance_tsukkomi(
            user_text_for_prompt,
            recent_bot_utterances,
        )
    if is_self_tsukkomi:
        deliberation_info["is_breakdown"] = True
        deliberation_info["breakdown_reason"] = f"直前のAI自身の発言（「{matched_kw}…」）に対するツッコミ・矛盾の指摘"
        intent_info["target_is_ai"] = True
    # 予算計算用に感情状態テキストの文字数を計測する（実際に削減はしない）
    _emotion_state_parts = build_agent_persona_parts(emotion_state, core_beliefs)
    _emotion_state_chars = sum(len(line) for line in _emotion_state_parts)
    # working_context を個別上限でトリムしてから予算管理へ渡す
    _working_prompt_lines_raw = ollama_helpers.truncate_lines(
        format_working_context_for_prompt(working_ctx),
        max_lines=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_LINES", 6) or 6), 2),
        max_chars_per_line=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_CHARS_PER_LINE", 180) or 180), 80),
        max_total_chars=max(int(cfg("OLLAMA_PROMPT_WORKING_MAX_TOTAL_CHARS", 420) or 420), 120),
    )
    (
        context_lines,
        memory_lines,
        channel_summary,
        last_assistant_text,
        habit_profile_lines,
        episode_lines,
        working_prompt_lines,
    ) = _trim_prompt_inputs(
        context_lines=context_lines,
        memory_lines=memory_lines,
        channel_summary=channel_summary,
        last_assistant_text=last_assistant_text,
        habit_profile_lines=habit_profile_lines,
        episode_lines=episode_lines,
        working_prompt_lines=_working_prompt_lines_raw,
        emotion_state_chars=_emotion_state_chars,
    )

    if channel_summary and not external_research_request:
        parts += ["", "最近の会話要約:", channel_summary]

    parts += build_agent_persona_parts(emotion_state, core_beliefs)

    if working_prompt_lines and not external_research_request:
        parts += [""]
        parts.extend(working_prompt_lines)

    if habit_profile_lines:
        parts += [
            "",
            "【相手ごとの会話習慣プロファイル】",
            "※この相手に合わせるための軽い傾向です。直近の発言内容と矛盾する時は、直近の発言を優先してください。",
        ]
        parts.extend([f"- {line}" for line in habit_profile_lines])

    if memory_lines:
        parts += [
            "",
            "【この相手に関係すると確認できた記憶】",
            "※対象メッセージに直接役立つ時だけ使い、関係ない固有名詞・人物・作品名・過去話題は持ち込まないでください。",
            "※外部調査結果やURLの内容に関連する話題（登場人物・作品・設定など）が含まれる場合は、知っている話題として自然に関連付けて言及してください。",
            "※「私の記憶によると」や「データによると」といった表現は避けてください。",
            f"※過去の記憶テキスト内に「私」という一人称が含まれていても、発言する際は必ずあなた自身の設定した一人称（例: {bot_identity.first_person}）に変換してください。絶対に『私』を使わないでください。",
            "※記憶内の情報は相手ユーザーに関係する内容です。あなた自身の名前や属性と混同しないでください。",
        ]
        parts.extend([f"- {line}" for line in memory_lines])

    if episode_lines and not external_research_request:
        parts += [
            "",
            "【関連する出来事の記憶】",
            "※相手が前の出来事や続きに触れている時だけ参照し、無理に持ち出さないでください。",
            "※記憶内の「私」という一人称には釣られず、必ず設定された自身の一人称を維持してください。",
        ]
        parts.extend([f"- {line}" for line in episode_lines])

    if (
        recent_bot_utterances
        and not external_research_request
        and not is_reply_to_other_user
        and not is_replying_to_old_message
    ):
        parts += [
            "",
            "【直近の自分の発言】",
        ]
        if is_hearsay:
            parts += [
                "※直前にあなた自身が発言した内容です。",
                f"※最新のユーザー発言は第三者『{hearsay_subject}』さんの過去の発言・伝聞についての報告です。あなた（AI）自身が言ったことではありません。「そんなこと言ったっけ？」「言った覚えがない」など、自分が発言したかのように勘違いして弁明・否定しないでください。",
            ]
        else:
            parts += [
                "※直前にあなた自身が発言した内容です。ユーザーがそれに対して質問・確認・ツッコミ・詳細要求（「どういうこと？」「何があったの？」「それ何？」「教えて」等）をしてきた場合は、自分自身が言ったこととして自覚し、「覚えてない」「何のことか分からない」とシラを切らずに、自分が振った話題やその背景を踏まえて自然に答えてください。最新のユーザー発言が前の話題に触れていないなら、その話題は引きずらずに捨ててください。重複や言い直しは避けてください。",
            ]
        parts.extend([f"- {line}" for line in recent_bot_utterances])

    if last_assistant_text and not is_replying_to_old_message and not is_reply_to_other_user:
        parts += ["", "直前のAI発言:", last_assistant_text]
        if external_research_request:
            parts += ["※最新メッセージや外部調査の内容を最優先してください。直前のAI発言と直接関連がある場合のみ参照してください。"]
        else:
            has_subsequent_user_messages = any(
                not line.strip().startswith("Assistant:")
                for line in context_lines[-3:]
                if ":" in line
            ) if context_lines else False
            if has_subsequent_user_messages:
                parts += [
                    "※直前のAI発言の後に他のユーザーの発言が続いています。最新発言がそれら直前の会話の流れに対するツッコミや反応である場合は、AI発言への回答ではなく現在の会話の流れを最優先してください。"
                ]
            else:
                parts += ["※最新のユーザー発言がこれに直接答えていないなら、この話題は完全に捨ててください。"]

    if media_prompt_parts:
        parts += list(media_prompt_parts)

    if context_lines:
        if external_research_request:
            recent_context = context_lines[-3:]
            parts += [
                "",
                "直前の会話文脈（参考）:",
                *recent_context,
                "※外部調査結果やURLの内容を最優先で回答してください。ただし、直前の会話や対象メッセージと関連する話題（登場人物や作品、前後の文脈など）があれば自然に触れて構いません。無関係な話題は混ぜないでください。",
            ]
        else:
            parts += ["", "会話履歴:", *context_lines]

    current_user_name = display_name(message)
    _non_user_prefixes = ("assistant", "system", "ai", "bot", "あなた", "内心のメモ")

    if reply_chain and latest_ref is not None:
        ref_content = content(latest_ref)
        ref_author_name = display_name(latest_ref)
        if ref_content:
            tree_lines = _format_reply_tree_context(
                reply_chain,
                bot_user_id=bot_user_id,
                cached_parent_turn=cached_reply_parent,
            )
            if is_reply_to_other_user:
                parts += [
                    "",
                    "【返信・会話関係】",
                    f"※現在『{current_user_name}』さんは、別ユーザー『{ref_author_name}』さんの発言（「{ref_content}」）に対してDiscordの返信（Reply）機能で話しかけています。",
                    f"※文中の代名詞（君、お前、あなた等）や命令・呼びかけはすべて『{ref_author_name}』さんを指しています。",
                ]
                if len(tree_lines) >= 2:
                    parts += [
                        "【返信ツリーの流れ】",
                        *tree_lines,
                    ]
            else:
                parts += [
                    "",
                    "【返信・会話関係（リプライの会話ツリー）】",
                    f"※現在『{current_user_name}』さんは、あなた（AI）の直前の発言（「{ref_content}」）に対して返信しています。",
                ]
                if len(tree_lines) >= 2:
                    parts += [
                        "【返信ツリーの流れ】",
                        *tree_lines,
                        "↓（今回の返信）",
                        f"『{current_user_name}』: 「{content(message)}」",
                        "※重要指示: 今回の発言において主語や目的語（「何を」「何が」等）が省略されている場合（例: 『食べる派ですか？』『どう思う？』等）、直前のチャンネル全体の雑談ではなく、必ずこの【返信ツリーの流れ】にある話題を最優先で引き継いで解釈・返答してください。",
                        "※直前にチャンネル内で別の話題（雑談）が流れていても、話題を混同したり、勝手に別の話題にすり替えたりしないでください。",
                    ]
                else:
                    parts += [
                        f"※重要指示: 今回の発言はあなたへのDiscord返信（Reply）です。主語や目的語が省略されている場合は、直前のチャンネル雑談ではなく、返信先の発言（「{ref_content}」）の文脈を最優先で引き継いで答えてください。",
                    ]

    other_users_in_context = {
        line.split(":", 1)[0].strip()
        for line in context_lines
        if ":" in line
        and not any(line.strip().lower().startswith(p) for p in _non_user_prefixes)
        and line.split(":", 1)[0].strip() and line.split(":", 1)[0].strip() != current_user_name
    }
    if latest_ref is not None and is_reply_to_other_user:
        ref_name = display_name(latest_ref).strip()
        if ref_name and ref_name != current_user_name and not any(ref_name.lower().startswith(p) for p in _non_user_prefixes):
            other_users_in_context.add(ref_name)

    if other_users_in_context:
        other_names = "、".join(sorted(other_users_in_context)[:3])
        parts += [
            "",
            "【複数人会話の文脈指示】",
            f"※現在返答する相手は『{current_user_name}』です。",
            f"※他の登場人物（{other_names}）の個人設定や記憶を『{current_user_name}』のものと混同しないでください。",
            f"※他の登場人物（{other_names}等）の名前で呼びかけたり、話しかける相手を取り違えたりしないでください。相手の名前を呼ぶ場合は必ず『{current_user_name}』を使ってください。",
            "※ただし、直前の他ユーザーの発言に対するツッコミや反応、喧嘩・煽りなどの会話の流れは把握し、必要に応じてそのやり取りを踏まえてリアクションしてください。",
        ]

    if mentioned_parts:
        parts += ["", *mentioned_parts]

    reactions_suffix, reactions_list = format_message_reactions(message, bot_user_id=bot_user_id)
    if is_reply_to_other_user and latest_ref is not None:
        ref_author_name = display_name(latest_ref)
        parts += [
            "",
            "【会話の状況（ユーザー同士のやり取り）】",
            f"発言者: {display_name(message)} （※別ユーザー『{ref_author_name}』さんへの返信）",
            f"返信先: {ref_author_name}: 「{content(latest_ref)}」",
            f"対象メッセージ: {display_name(message)}（→ {ref_author_name}への返信）: {content(message)}{reactions_suffix}",
            "※注意: 『〇〇さんに聞かれてるんですよ』『私宛てじゃない』『私じゃなくて』のような状況のメタ説明や客観解説は絶対に口に出さないでください。二人のやり取りの空気に合わせて外野から自然に相槌やツッコミ（共感、見守り、煽り、戸惑い等）を返してください。",
        ]
    else:
        parts += [
            "",
            "【現在の対話相手】",
            f"ユーザー名: {display_name(message)}",
            "※あなたが今話している相手です。相手との距離感を自然に保ってください。",
            f"※相手を呼ぶ・お礼を言う場合は、必ず現在の対話相手の名前（『{display_name(message)}』さん等）を使ってください。あなた自身の名前（{bot_identity.primary_name}等）で相手を呼ぶこと（例: 『ありがとう、{bot_identity.primary_name}』）は絶対に禁止です。",
            f"※ユーザーが発言内であなた（{bot_identity.primary_name}）を呼んだ場合でも、それはあなたへの呼びかけであり、相手の名前ではありません。",
            f"※どんなに驚いたり、動揺したり、ショックを受けた場合でも、一人称は必ず設定されたもの（{bot_identity.first_person}）を維持してください。絶対に『私』『あたし』を使わないでください。",
            "",
            "【対象メッセージ】",
            f"{display_name(message)}: {content(message)}{reactions_suffix}",
        ]
    bot_reactions = [r for r in reactions_list if r.get("is_bot")]
    if bot_reactions:
        react_descs = []
        for br in bot_reactions:
            category = br.get("category", "")
            emoji = br.get("emoji", "")
            if category == "GOOD":
                react_descs.append(f"GOOD（好感・同意の絵文字 {emoji}）")
            elif category == "BAD":
                react_descs.append(f"BAD（不快・否定の絵文字 {emoji}）")
            else:
                react_descs.append(f"絵文字 {emoji}")
        parts += [
            "",
            "【あなたのリアクション】",
            f"※あなたはこのメッセージに対して「{'、'.join(react_descs)}」でリアクションしました。リアクションした事実を踏まえて自然に返答してください。",
        ]

    if is_hearsay:
        parts += [
            "",
            "【第三者の発言に関する報告・伝聞】",
            f"※最新のユーザー発言は、第三者『{hearsay_subject}』さんが言っていた内容（「{hearsay_quote}」）についての報告・タレコミです。",
            "※あなた（AI）が言ったことではありません。絶対に「そんなこと言ったっけ？」「言った覚えがない」など、自分が言ったかのように勘違いして弁明・否定しないでください。",
            f"※『{hearsay_subject}』さんがそんなことを言っていた事実に対して、驚き、戸惑い、ショック、あるいは反論・ツッコミなど、報告を聞いた側（第三者の発言を受けた側）としての自然なリアクションを返してください。",
        ]

    parts += build_user_prompt_extra_rule_parts()

    if bool(intent_info.get("target_is_ai")) and str(intent_info.get("intent", "")).lower() in {"request", "依頼", "要求", "command", "指示"}:
        parts += build_user_prompt_target_is_ai_parts()
    if bool(intent_info.get("should_clarify")):
        parts += [
            "",
            "追加指示:",
            "相手の意図が少し飛躍しています。",
            "言葉の定義をメタ的に問い詰めるのではなく、キャラクター本人の知識不足や単純な疑問として、自然に軽く聞き返してください。",
        ]

    if deliberation_info.get("persona_risk") and deliberation_info.get("consistency_correction"):
        parts += [
            "",
            "【自己整合性による強い警告】",
            f"※ {deliberation_info['consistency_correction']}",
            "※ この警告を最優先し、相手の発言やペースに飲まれず、いかなる場合もあなたのキャラクター設定（口調・人格・立場）を崩さないでください。",
        ]

    if deliberation_info.get("is_breakdown"):
        reason = deliberation_info.get("breakdown_reason") or "不明"
        parts += [
            "",
            "【会話修復の指示】",
            f"※ 相手は直前のやり取りに戸惑いやズレ、不満を感じています（理由: {reason}）。",
            "※ ここで無理に言い負かしたりせず、すっと意図を汲み直して「ああ、そういうことか」「勘違いしてた」と自然に誤解を解き、会話の軌道修正を行ってください。",
        ]

    if is_self_tsukkomi:
        matched_short = ollama_helpers.truncate_text(matched_bot_utterance.replace("\n", " "), 50)
        clean_user_display = ollama_helpers.truncate_text(content(message) or user_text_for_prompt, 40)
        parts += [
            "",
            "【直近の自発言へのツッコミ・確認】",
            f"※ 相手の最新発言（「{clean_user_display}」）は、あなた自身の直近の発言（「{matched_short}」）に対するツッコミや矛盾の指摘です。",
            "※ 「何のことかわからない」「時差があるんですか？」などと他人事のように扱ったり相手の勘違いに仕立て上げたりせず、自分自身の直近の発言を自覚し、「さっき〜って言っちゃったけど」「勘違いしてた」と自然に誤解・言い間違いを認めてリアクションしてください。",
        ]

    uncertainty = deliberation_info.get("uncertainty_level")
    if uncertainty in ("high", "medium") and not is_hearsay:
        parts += [
            "",
            "【不確実性の表現】",
            "※ この話題についてあなたは記憶が曖昧か、確信が持てていません。",
            "※ 知ったかぶりや断言を避け、「うろ覚えなんだけど」「違ったらごめん」といった前置きを自然に使って自信のなさを表現してください。",
        ]

    if deliberation_info.get("needs_clarification"):
        parts += [
            "",
            "【聞き返しの指示】",
            "※ 相手の意図が少し曖昧です。",
            "※ わかったふりをして断定せず、キャラクターが直感的に感じた疑問や戸惑いをそのまま言葉にして、短く確認をとってください。",
        ]

    dm_channel_cls = getattr(discord, "DMChannel", None)
    is_dm = isinstance(message.channel, dm_channel_cls) if dm_channel_cls is not None else False
    tension = getattr(emotion_state, "tension", 0.0) if emotion_state else 0.0
    if is_dm:
        parts += [
            "",
            "【感情表現の指示（開放）】",
            "現在は1対1のDM空間です。内心の感情をある程度ストレートに出して構いません。",
        ]
    elif tension > 0.6:
        parts += [
            "",
            "【感情表現の指示（抑制）】",
            "現在は公開の場であり、警戒が必要です。内心の過度な感情（怒りなど）はそのまま表に出さず、状況をわきまえた大人の対応を維持してください。",
        ]

    extra_reply_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
    if extra_reply_rule:
        parts.append(extra_reply_rule)
    parts += ["", "※重要: あなたは「Assistant」です。Assistantとしての返答本文のみを出力し、名前の接頭辞は出力しないでください。"]
    prompt_state = {
        "working_context": working_ctx,
        "working_prompt_lines": working_prompt_lines,
        "pending_intents": pending_intents,
        "episode_lines": episode_lines,
        "raw_memory_lines": raw_memory_lines,
        "habit_profile_lines": habit_profile_lines,
        "channel_summary": channel_summary,
    }

    raw_prompt = "\n".join(parts).strip()
    final_prompt = append_chat_reply_output_suffix(raw_prompt)
    return final_prompt, handled_message_ids, context_lines, last_assistant_text, intent_info, deliberation_info, prompt_state
