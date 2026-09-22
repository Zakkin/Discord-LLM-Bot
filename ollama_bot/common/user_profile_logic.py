"""
user_profile_logic.py

Discordサーバー内のメンバープロファイル（現在名、過去名・エイリアス、ロール等）を
会話テキストから検出し、プロンプト向けに解決・フォーマットするロジックを提供します。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from lib.discord_utils import clean_user_input_text
from ..ollama_chat_.ollama_chat_types import UserRelationship

log = logging.getLogger("ollama_bot.common.user_profile")


_GENERIC_MENTION_STOPWORDS = {
    "私", "わたし", "オレ", "おれ", "僕", "ぼく", "自分", "お前", "あんた", "君", "きみ", "あなた",
    "誰", "だれ", "何", "なに", "やつ", "奴", "人", "みんな", "全員", "誰か",
    "bot", "ボット", "ai", "assistant",
    "これ", "それ", "あれ", "どれ", "ここ", "そこ", "あそこ", "どこ",
    "今日", "昨日", "明日", "今", "過去", "未来", "昔", "前",
}


def _get_mention_stopwords() -> set[str]:
    stopwords = set(_GENERIC_MENTION_STOPWORDS)
    from lib.config_utils import cfg
    aliases = cfg("BOT_ALIASES", ())
    if isinstance(aliases, (list, tuple, set)):
        stopwords.update(str(a).strip().lower() for a in aliases if a)
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip().lower()
    if first_person:
        stopwords.add(first_person)
    return stopwords


def _clean_text_for_search(text: str) -> str:
    return clean_user_input_text(text, remove_urls=True)


def resolve_mentioned_users(
    text: str,
    profiles: list[dict[str, Any]],
    *,
    exclude_user_id: int | None = None,
    exclude_names: set[str] | list[str] | None = None,
) -> list[dict[str, Any]]:
    """会話テキストから言及されているサーバーメンバー（現在名または過去名）を抽出・特定する。"""
    raw_text = _clean_text_for_search(text)
    if not raw_text.strip() or not profiles:
        return []

    lowered_text = raw_text.lower()
    resolved: list[dict[str, Any]] = []
    stopwords = _get_mention_stopwords()
    seen_user_ids: set[int] = set()
    seen_display_names: set[str] = set()

    if exclude_user_id is not None:
        seen_user_ids.add(int(exclude_user_id))

    exclude_names_clean: set[str] = {
        str(n).strip().lower() for n in (exclude_names or []) if str(n).strip()
    }

    # 長い名前から優先的にマッチさせるためソート
    candidates: list[tuple[str, bool, dict[str, Any]]] = []
    for prof in profiles:
        uid = int(prof.get("user_id") or 0)
        if uid in seen_user_ids or uid == 0:
            continue

        disp = str(prof.get("current_display_name") or "").strip()
        name = str(prof.get("current_name") or "").strip()
        aliases = [str(a).strip() for a in (prof.get("aliases") or []) if str(a).strip()]

        if (
            disp
            and len(disp) >= 2
            and disp.lower() not in _GENERIC_MENTION_STOPWORDS
            and disp.lower() not in exclude_names_clean
        ):
            candidates.append((disp, False, prof))
        if (
            name
            and len(name) >= 2
            and name.lower() not in _GENERIC_MENTION_STOPWORDS
            and name.lower() != disp.lower()
            and name.lower() not in exclude_names_clean
        ):
            candidates.append((name, False, prof))
        for alias in aliases:
            if (
                alias
                and len(alias) >= 2
                and alias.lower() not in _GENERIC_MENTION_STOPWORDS
                and alias.lower() not in exclude_names_clean
            ):
                candidates.append((alias, True, prof))

    # 名前の文字数降順でソート
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    for term, is_alias, prof in candidates:
        uid = int(prof.get("user_id") or 0)
        disp_name = str(prof.get("current_display_name") or prof.get("current_name") or f"user_{uid}").strip()
        if uid in seen_user_ids or disp_name.lower() in seen_display_names:
            continue

        term_lower = term.lower()
        if term_lower in exclude_names_clean or disp_name.lower() in exclude_names_clean:
            continue

        # 単語境界または単純部分一致の確認
        # 日本語の場合は部分一致、アルファベットのみの場合は単語境界を考慮
        if re.search(r"^[a-zA-Z0-9_]+$", term):
            pattern = rf"(?<![a-zA-Z0-9_]){re.escape(term_lower)}(?![a-zA-Z0-9_])"
            matched = bool(re.search(pattern, lowered_text))
        else:
            matched = term_lower in lowered_text

        if matched:
            seen_user_ids.add(uid)
            seen_display_names.add(disp_name.lower())
            resolved.append({
                "user_id": uid,
                "current_display_name": disp_name,
                "current_name": str(prof.get("current_name") or ""),
                "matched_name": term,
                "is_alias": is_alias,
                "aliases": list(prof.get("aliases") or []),
                "roles": list(prof.get("roles") or []),
            })

    return resolved[:3]


_META_CONFUSION_PATTERNS: tuple[str, ...] = (
    "名前を聞かれ",
    "名前を出され",
    "名前を言われ",
    "名前を呼ばれ",
    "名前を聞いて",
    "名前を出して",
    "急に名前",
    "突然名前",
    "誰のことか",
    "誰ですか",
    "何を考えているのかよくわからない",
    "何を考えてるのか読めない",
    "意図が掴めない",
    "意図がつかめない",
    "意図がわからない",
    "見覚えがない",
    "情報がない",
    "知らない人",
)


def is_meta_confusion_thought(thought: str | None) -> bool:
    """thought が人物評ではなく、名前を聞かれたこと等に対する一時的なメタ困惑・戸惑いであるかを判定する。"""
    cleaned = str(thought or "").strip()
    if not cleaned:
        return True
    for p in _META_CONFUSION_PATTERNS:
        if p in cleaned:
            return True
    if re.search(r"(?:名前|誰か).*?(?:聞かれ|出され|言われ|呼ばれ).*?(?:戸惑|わから|掴め|つかめ|困惑|意図)", cleaned):
        return True
    return False


def describe_relationship_for_third_party(rel: UserRelationship | None) -> str:
    """第三者（話題に出たメンバー）に対するBotの等身大の心情・人物像を自然な日本語で説明する。"""
    if rel is None:
        return "まだあまり直接お話ししたことがなく、少し緊張しながら接しているお知り合い。"

    aff = float(getattr(rel, "affinity", 0.0) or 0.0)
    trust = float(getattr(rel, "trust", 0.0) or 0.0)
    affection = float(getattr(rel, "affection", 0.0) or 0.0)
    hatred = float(getattr(rel, "hatred", 0.0) or 0.0)

    # 敵対・強い警戒（例: 雑菌のような aff=-3.8, trust=-5.7, hatred=+1.0 など）
    if hatred >= 4.0 or (aff <= -4.0 and trust <= -4.0):
        return "強い敵対心や警戒感があり、意地悪や攻撃が怖くて関わるのを避けたい相手。"
    if trust <= -2.0 or aff <= -2.0:
        return "いつもからかわれたり意地悪や棘のあることを言われがちで、かなり身構えて警戒している相手。あまり信用できず距離を置きたいと思っている。"
    if trust <= -0.8 or aff <= -0.8:
        return "少し言葉の裏を疑って身構えてしまいがちで、まだ警戒を解けていない相手。"

    # 愛憎・複雑
    if affection >= 2.0 and hatred >= 2.0:
        return "腹が立つことや意地悪も多いけれど、どこか気になって放っておけない複雑な相手。"

    # 親密・信頼
    if affection >= 4.0 and trust >= 3.0:
        return "心から安心できて頼りにしている、かけがえのない大切な大好きな常連さん。"
    if aff >= 3.0 and trust >= 2.0:
        return "とても親しみを感じていて、いつも楽しく安心して話せる仲良しの常連さん。"
    if aff >= 1.0 or trust >= 1.0:
        return "好意的で話しやすく、優しく接してくれる良い人だと思っている。"

    return "まだ知り合って日が浅く、たまに会話する程度で少し緊張しながら接しているお知り合い。"


def format_mentioned_users_for_prompt(
    resolved_users: list[dict[str, Any]],
    relationships: dict[int, UserRelationship] | None = None,
    user_interests: list[dict[str, Any]] | None = None,
    user_text: str = "",
    context_lines: list[str] | None = None,
    user_memories: dict[int, list[str]] | None = None,
    current_user_name: str = "",
) -> list[str]:
    """解決されたメンバー情報をプロンプト向けテキストに整形する。"""
    if not resolved_users:
        return []

    lines: list[str] = [
        "【会話中で言及されたサーバーメンバー】",
    ]

    seen_disps: set[str] = set()
    for item in resolved_users:
        uid = item["user_id"]
        curr_disp = item["current_display_name"]
        if curr_disp.lower() in seen_disps:
            continue
        seen_disps.add(curr_disp.lower())

        matched = item["matched_name"]
        is_alias = item["is_alias"]

        if is_alias and matched.lower() != curr_disp.lower():
            desc = f"- 「{matched}」: サーバーメンバー『{curr_disp}』さんの過去の名前（ニックネーム）です。"
        elif matched.lower() != curr_disp.lower():
            desc = f"- 「{matched}」: サーバーメンバー『{curr_disp}』さんのことです。"
        else:
            desc = f"- サーバーメンバー『{curr_disp}』さんについて言及されています。"

        lines.append(desc)

        # 直近会話ログに参加していたかの注記
        in_recent_context = False
        if context_lines:
            for cl in context_lines[-6:]:
                if curr_disp in cl or (matched and matched in cl):
                    in_recent_context = True
                    break
        if in_recent_context:
            lines.append(f"  - ※このメンバー（『{curr_disp}』さん）は直前の会話にも参加しており、さっきまで話していた相手です。")

        # 関係値情報
        rel = relationships.get(uid) if relationships else None
        aff = float(getattr(rel, "affinity", 0.0) or 0.0) if rel else 0.0
        trust = float(getattr(rel, "trust", 0.0) or 0.0) if rel else 0.0
        affection = float(getattr(rel, "affection", 0.0) or 0.0) if rel else 0.0
        hatred = float(getattr(rel, "hatred", 0.0) or 0.0) if rel else 0.0
        party_impression = describe_relationship_for_third_party(rel)

        rel_info = f"  - あなたとの関係性: 親愛度 {aff:+.1f}, 信頼度 {trust:+.1f}, 好意 {affection:+.1f}, 憎しみ {hatred:+.1f}"
        lines.append(rel_info)
        lines.append(f"  - あなたから見たこの人の人物像: {party_impression}")

        raw_thought = str(getattr(rel, "thought", "") or "").strip() if rel else ""
        if raw_thought and not is_meta_confusion_thought(raw_thought):
            lines.append(f"  - あなたの印象（本音の一言）: 「{raw_thought}」")

        # 関連する関心事情報
        if user_interests:
            matched_interests = [
                str(ui.get("topic", "")).strip()
                for ui in user_interests
                if int(ui.get("user_id") or 0) == uid and str(ui.get("topic", "")).strip()
            ]
            if matched_interests:
                lines.append(f"  - この人の関心事・話題: {', '.join(matched_interests[:3])}")

        # 関連する記憶情報
        if user_memories and uid in user_memories:
            mems = [m for m in user_memories[uid] if m]
            if mems:
                lines.append(f"  - この人に関する過去の話題・エピソード: {' / '.join(mems[:2])}")

    lines.append("※上記メンバーは【会話の中で話題に挙がった人物】であり、現在の対話相手ではありません。")
    lines.append("※返答の冒頭などで上記メンバーの名前を呼びかけたり、話しかける相手と混同したりしないでください。相手の名前を呼ぶ場合は必ず【現在の対話相手】の名前を使ってください。")

    is_opinion_query = any(
        q in user_text for q in (
            "どう思う", "どう考える", "どう？", "どうですか", "どうかな",
            "知ってる", "知ってます", "知ってる事", "知ってること", "教えて", "教えろ",
            "どんな人", "誰", "だれ", "どんな奴",
        )
    ) if user_text else False

    if is_opinion_query:
        lines.append("【最重要: 上記メンバーへの質問に対する返答指示】")
        for item in resolved_users:
            curr_disp = item["current_display_name"]
            uid = item["user_id"]
            rel = relationships.get(uid) if relationships else None
            party_impression = describe_relationship_for_third_party(rel)
            raw_thought = str(getattr(rel, "thought", "") or "").strip() if rel else ""
            thought_text = raw_thought if (raw_thought and not is_meta_confusion_thought(raw_thought)) else ""

            target_hint = f"対話相手（{current_user_name}さん）" if current_user_name else "対話相手"
            lines.append(f"※{target_hint}は、サーバーメンバー『{curr_disp}』さん（話題の第三者）が普段どんな人か、あなたの印象・感想を尋ねています。")
            lines.append(f"  - あなたから見た『{curr_disp}』さんの普段の人物像: {party_impression}")
            if thought_text:
                lines.append(f"  - あなたが普段感じている本音: 「{thought_text}」")

        lines.append("※【禁止事項】『見覚えがない』『知らない』『情報がない』『誰ですか？』『詳しく教えて』等と辞書的にシラを切ったり突き放したりすることは絶対に禁止です。『名前を聞かれても意図が掴めない』等の言い訳をしてはいけません。")
        lines.append("※上記の人物像や関係性（警戒している、仲良し、怖い等）、関連するエピソード記憶に基づいて、あなた自身の言葉で『その人に対する等身大の感想・気持ち』を自然に答えてください。")
    else:
        lines.append("※相手が上記メンバーについて質問したり感想・知っていることを求めている場合は、『情報がない』『知らない』と冷たく辞書的に突っぱねず、上記の関係性やあなたの印象、関連する記憶に基づいてあなたの言葉で自然に答えてください。")
    return lines


async def resolve_and_format_mentioned_users(
    *,
    user_text: str,
    context_lines: list[str],
    guild_id: int | None,
    exclude_user_id: int | None,
    exclude_names: set[str] | list[str] | None = None,
    memory_store: Any,
    user_relationships: dict[int, UserRelationship] | None = None,
    cfg_fn: Any = None,
    current_user_name: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """サーバーメンバーの言及を解決し、(解決されたユーザー一覧, プロンプト用パーツ一覧) を返す。"""
    if guild_id is None or not hasattr(memory_store, "list_user_profiles_for_guild"):
        return [], []

    try:
        profiles = await memory_store.list_user_profiles_for_guild(guild_id=int(guild_id), limit=200)
        if not profiles:
            return [], []

        search_text = f"{user_text}\n{' '.join(context_lines[-3:])}"
        resolved_users = resolve_mentioned_users(
            search_text,
            profiles,
            exclude_user_id=exclude_user_id,
            exclude_names=exclude_names,
        )
        if not resolved_users:
            return [], []

        persona = "default"
        if cfg_fn is not None and callable(cfg_fn):
            persona = str(cfg_fn("MEMORY_PERSONA_NAMESPACE", "default") or "default")

        user_interests_list = None
        if hasattr(memory_store, "list_user_interests"):
            user_interests_list = await memory_store.list_user_interests(
                persona=persona,
                guild_id=int(guild_id),
                limit=20,
            )

        # 該当メンバーに関するエピソード記憶の検索
        user_memories: dict[int, list[str]] = {}
        if hasattr(memory_store, "search_memories_by_keyword"):
            for ru in resolved_users:
                ru_uid = ru["user_id"]
                ru_disp = ru["current_display_name"]
                try:
                    mem_results = await memory_store.search_memories_by_keyword(
                        ru_disp,
                        persona=persona,
                        guild_id=int(guild_id),
                        limit=3,
                    )
                    if mem_results:
                        user_memories[ru_uid] = [
                            str(m.get("content", "")).strip()
                            for m in mem_results
                            if str(m.get("content", "")).strip()
                        ]
                except Exception as mem_err:
                    log.debug("search_memories_by_keyword failed for user %d: %r", ru_uid, mem_err)

        for ru in resolved_users:
            ru_uid = ru["user_id"]
            ru_disp = ru["current_display_name"]
            ru_rel = user_relationships.get(ru_uid) if user_relationships else None
            ru_thought = str(getattr(ru_rel, "thought", "") or "").strip() if ru_rel else ""
            ru_aff = float(getattr(ru_rel, "affinity", 0.0) or 0.0) if ru_rel else 0.0
            ru_trust = float(getattr(ru_rel, "trust", 0.0) or 0.0) if ru_rel else 0.0
            ru_hatred = float(getattr(ru_rel, "hatred", 0.0) or 0.0) if ru_rel else 0.0
            ru_imp = describe_relationship_for_third_party(ru_rel)
            ru_mems = user_memories.get(ru_uid, [])
            log.info(
                "[TRACE mentioned_user] resolved member=%r (uid=%d, affinity=%+.1f, trust=%+.1f, hatred=%+.1f, thought=%r, is_meta=%s, impression=%r, memories=%d)",
                ru_disp, ru_uid, ru_aff, ru_trust, ru_hatred, ru_thought, is_meta_confusion_thought(ru_thought), ru_imp, len(ru_mems),
            )

        parts = format_mentioned_users_for_prompt(
            resolved_users,
            relationships=user_relationships,
            user_interests=user_interests_list,
            user_text=user_text,
            context_lines=context_lines,
            user_memories=user_memories,
            current_user_name=current_user_name,
        )
        return resolved_users, parts
    except Exception as e:
        log.warning("resolve_and_format_mentioned_users failed: %r", e)
        return [], []

