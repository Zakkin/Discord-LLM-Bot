"""
user_profile_logic.py

Discordサーバー内のメンバープロファイル（現在名、過去名・エイリアス、ロール等）を
会話テキストから検出し、プロンプト向けに解決・フォーマットするロジックを提供します。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from lib.discord_utils import clean_user_input_text, strip_discord_mentions
from ollama_bot.ollama_chat_.ollama_chat_types import UserRelationship

log = logging.getLogger("ollama_bot.common.user_profile")

_ANAPHORA_PRONOUNS_PATTERN = re.compile(
    r"(彼|彼女|あいつ|あいつら|あの方|その人|その方|この人|この方|奴|やつ|あの子|あいつのこと|彼のこと|彼女のこと|その人のこと|この人のこと)"
)


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


def extract_guild_member_profiles(guild: Any) -> list[dict[str, Any]]:
    """DiscordのGuildオブジェクトからメンバープロファイルのリストを構築する。"""
    if not guild or not hasattr(guild, "members"):
        return []
    profiles: list[dict[str, Any]] = []
    try:
        guild_id_val = int(getattr(guild, "id", 0) or 0)
        for m in getattr(guild, "members", []):
            if getattr(m, "bot", False):
                continue
            uid = int(getattr(m, "id", 0) or 0)
            if uid == 0:
                continue
            name = str(getattr(m, "name", "") or "").strip()
            disp = str(getattr(m, "display_name", "") or getattr(m, "global_name", "") or name).strip()
            nick = str(getattr(m, "nick", "") or "").strip()
            aliases: list[str] = []
            if nick and nick != disp and nick != name:
                aliases.append(nick)
            global_name = str(getattr(m, "global_name", "") or "").strip()
            if global_name and global_name != disp and global_name != name and global_name not in aliases:
                aliases.append(global_name)
            roles = [
                str(getattr(r, "name", "") or "").strip()
                for r in getattr(m, "roles", [])
                if getattr(r, "name", "") and getattr(r, "name", "") != "@everyone"
            ]
            profiles.append({
                "guild_id": guild_id_val,
                "user_id": uid,
                "current_name": name,
                "current_display_name": disp,
                "aliases": aliases,
                "roles": roles,
            })
    except Exception as e:
        log.warning("extract_guild_member_profiles failed: %r", e)
    return profiles


def merge_guild_and_db_profiles(
    db_profiles: list[dict[str, Any]],
    guild: Any = None,
) -> list[dict[str, Any]]:
    """DBプロファイル一覧とDiscord Guildのメンバー一覧をマージする。"""
    if not guild:
        return list(db_profiles)
    guild_profs = extract_guild_member_profiles(guild)
    if not guild_profs:
        return list(db_profiles)

    merged: list[dict[str, Any]] = []
    seen_uids: set[int] = set()
    for p in db_profiles:
        uid = int(p.get("user_id") or 0)
        if uid > 0:
            seen_uids.add(uid)
        merged.append(dict(p))

    for gp in guild_profs:
        uid = int(gp.get("user_id") or 0)
        if uid not in seen_uids:
            seen_uids.add(uid)
            merged.append(gp)
        else:
            for existing in merged:
                if int(existing.get("user_id") or 0) == uid:
                    g_disp = gp.get("current_display_name")
                    g_name = gp.get("current_name")
                    existing_aliases = set(existing.get("aliases") or [])
                    if g_disp and g_disp != existing.get("current_display_name") and g_disp != existing.get("current_name"):
                        existing_aliases.add(g_disp)
                    if g_name and g_name != existing.get("current_name") and g_name != existing.get("current_display_name"):
                        existing_aliases.add(g_name)
                    for a in gp.get("aliases") or []:
                        existing_aliases.add(a)
                    existing["aliases"] = list(existing_aliases)
                    if not existing.get("roles") and gp.get("roles"):
                        existing["roles"] = list(gp.get("roles") or [])
                    break
    return merged



def _to_hiragana(text: str) -> str:
    """カタカナをひらがなに正規化する。"""
    return "".join(
        chr(ord(c) - 0x60) if "\u30a1" <= c <= "\u30f6" else c
        for c in (text or "")
    )


_JAPANESE_NAME_AFFIXES = {
    # 敬称
    "さん", "くん", "君", "ちゃん", "氏", "先生", "様", "どの",
    # 助詞・接続助詞・引用
    "は", "が", "を", "に", "へ", "と", "で", "から", "より", "の", "って", "とは",
    "のこと", "について", "に関する", "にも", "なら", "でも", "じゃ", "や", "ね", "よ",
    "たち", "等", "ら",
}


def _is_term_matched_with_boundary(term: str, text: str) -> bool:
    """
    クエリテキスト内に term（メンバー名やエイリアス）が自立した単語として出現しているか判定する。
    単語の途中（例: 'ワンパース' の中の 'パー'、熟語の一部など）を誤検出しないよう
    前後の文字種（カタカナ、漢字、英数字、ひらがな）の境界を検査する。
    """
    if not term or not text:
        return False
    term_lower = term.lower().strip()
    text_lower = text.lower().strip()
    if not term_lower or not text_lower:
        return False

    if term_lower == text_lower:
        return True

    # 英数字のみの場合: 単語境界
    if re.search(r"^[a-zA-Z0-9_]+$", term_lower):
        pattern = rf"(?<![a-zA-Z0-9_]){re.escape(term_lower)}(?![a-zA-Z0-9_])"
        return bool(re.search(pattern, text_lower))

    # カタカナのみ（長音符含む）の場合
    # 例: 'パー', 'ドラシャン'
    # 前後にカタカナ・長音符が隣接していれば、別のカタカナ単語の途中なので除外
    if re.fullmatch(r"[\u30a1-\u30f6ー]+", term_lower):
        pattern = rf"(?<![\u30a1-\u30f6ー]){re.escape(term_lower)}(?![\u30a1-\u30f6ー])"
        if re.search(pattern, text_lower):
            return True
        # ひらがな正規化もチェック
        term_hira = _to_hiragana(term_lower)
        text_hira = _to_hiragana(text_lower)
        pattern_hira = rf"(?<![\u3041-\u3096ー]){re.escape(term_hira)}(?![\u3041-\u3096ー])"
        return bool(re.search(pattern_hira, text_hira))

    # 漢字のみの場合
    # 例: '雑菌'
    # 前後に漢字が隣接していれば熟語の途中なので除外
    if re.fullmatch(r"[\u4e00-\u9fff]+", term_lower):
        pattern = rf"(?<![\u4e00-\u9fff]){re.escape(term_lower)}(?![\u4e00-\u9fff])"
        return bool(re.search(pattern, text_lower))

    # ひらがなのみの場合
    if re.fullmatch(r"[\u3041-\u3096ー]+", term_lower):
        term_hira = term_lower
        text_hira = _to_hiragana(text_lower)
        escaped = re.escape(term_hira)
        for m in re.finditer(escaped, text_hira):
            start, end = m.span()
            if start > 0 and re.match(r"[\u3041-\u3096ー]", text_hira[start - 1]):
                continue
            if end < len(text_hira):
                next_char = text_hira[end]
                if re.match(r"[\u3041-\u3096ー]", next_char):
                    rest = text_hira[end:]
                    if not any(rest.startswith(affix) for affix in _JAPANESE_NAME_AFFIXES):
                        continue
            return True
        return False

    # 複合文字種（例: 漢字＋ひらがな、英数字＋カタカナ等）
    escaped = re.escape(term_lower)
    for m in re.finditer(escaped, text_lower):
        start, end = m.span()
        if start > 0:
            prev_char = text_lower[start - 1]
            first_char = term_lower[0]
            if re.match(r"[\u30a1-\u30f6ー]", first_char) and re.match(r"[\u30a1-\u30f6ー]", prev_char):
                continue
            if re.match(r"[\u4e00-\u9fff]", first_char) and re.match(r"[\u4e00-\u9fff]", prev_char):
                continue
            if re.match(r"[a-zA-Z0-9_]", first_char) and re.match(r"[a-zA-Z0-9_]", prev_char):
                continue
            if re.match(r"[\u3041-\u3096ー]", first_char) and re.match(r"[\u3041-\u3096ー]", prev_char):
                continue
        if end < len(text_lower):
            next_char = text_lower[end]
            last_char = term_lower[-1]
            if re.match(r"[\u30a1-\u30f6ー]", last_char) and re.match(r"[\u30a1-\u30f6ー]", next_char):
                continue
            if re.match(r"[\u4e00-\u9fff]", last_char) and re.match(r"[\u4e00-\u9fff]", next_char):
                continue
            if re.match(r"[a-zA-Z0-9_]", last_char) and re.match(r"[a-zA-Z0-9_]", next_char):
                continue
            if re.match(r"[\u3041-\u3096ー]", last_char) and re.match(r"[\u3041-\u3096ー]", next_char):
                rest = text_lower[end:]
                if not any(rest.startswith(affix) for affix in _JAPANESE_NAME_AFFIXES):
                    continue
        return True

    return False


def find_matching_guild_member(
    query: str,
    profiles: list[dict[str, Any]],
    *,
    exclude_user_id: int | None = None,
    exclude_names: set[str] | list[str] | None = None,
) -> dict[str, Any] | None:
    """
    指定された語句や文（例: '雑菌', 'ブルードラシャン', '雑菌についてどう思う？'）から
    サーバーメンバーに該当する人物を優先照合する。
    完全一致、敬称除去一致、エイリアス一致、カタカナ/ひらがな正規化一致を順に検証する。
    """
    clean_q = _clean_text_for_search(query).strip()
    if not clean_q or not profiles:
        return None

    exclude_names_clean: set[str] = {
        str(n).strip().lower() for n in (exclude_names or []) if str(n).strip()
    }

    # 文末の疑問表現や助詞、敬称を除去したコア語幹候補
    core_q = re.sub(
        r"(?:について|って|とは|は|の|のこと|に関する)?(?:どう|どう思う|どんな|教えて|誰|なに|何)?(?:[?？!！\s]|)$",
        "",
        clean_q,
    ).strip()
    core_q = re.sub(r"(?:さん|くん|君|ちゃん|氏|先生)$", "", core_q).strip()
    if len(core_q) < 2:
        core_q = clean_q

    q_lower = clean_q.lower()
    core_lower = core_q.lower()
    q_hira = _to_hiragana(q_lower)
    core_hira = _to_hiragana(core_lower)

    stopwords = _get_mention_stopwords()

    # 1. コア語幹またはクエリ全体との完全一致（表示名または現在名）
    for prof in profiles:
        uid = int(prof.get("user_id") or 0)
        if exclude_user_id is not None and uid == exclude_user_id:
            continue
        disp = str(prof.get("current_display_name") or "").strip()
        name = str(prof.get("current_name") or "").strip()
        disp_lower = disp.lower()
        name_lower = name.lower()

        if disp_lower in exclude_names_clean or name_lower in exclude_names_clean:
            continue
        if disp_lower in stopwords and name_lower in stopwords:
            continue

        disp_hira = _to_hiragana(disp_lower)
        name_hira = _to_hiragana(name_lower)

        if core_lower in (disp_lower, name_lower) or q_lower in (disp_lower, name_lower):
            return prof
        if core_hira in (disp_hira, name_hira) or q_hira in (disp_hira, name_hira):
            return prof

    # 2. 過去のエイリアスとの完全一致
    for prof in profiles:
        uid = int(prof.get("user_id") or 0)
        if exclude_user_id is not None and uid == exclude_user_id:
            continue
        disp_lower = str(prof.get("current_display_name") or "").strip().lower()
        if disp_lower in exclude_names_clean:
            continue
        aliases = [str(a).strip().lower() for a in (prof.get("aliases") or []) if str(a).strip()]
        for a in aliases:
            if a in exclude_names_clean or a in stopwords:
                continue
            a_hira = _to_hiragana(a)
            if core_lower == a or q_lower == a or core_hira == a_hira or q_hira == a_hira:
                return prof

    # 3. ユーザー名・表示名がクエリ文中に含まれているか（部分一致・名前の長さ降順）
    sorted_candidates: list[tuple[str, dict[str, Any]]] = []
    for prof in profiles:
        uid = int(prof.get("user_id") or 0)
        if exclude_user_id is not None and uid == exclude_user_id:
            continue
        disp = str(prof.get("current_display_name") or "").strip().lower()
        name = str(prof.get("current_name") or "").strip().lower()
        if disp in exclude_names_clean or name in exclude_names_clean:
            continue
        aliases = [str(a).strip().lower() for a in (prof.get("aliases") or []) if str(a).strip()]
        for term in [disp, name] + aliases:
            if term and len(term) >= 2 and term not in stopwords and term not in exclude_names_clean:
                sorted_candidates.append((term, prof))

    sorted_candidates.sort(key=lambda x: len(x[0]), reverse=True)
    for term, prof in sorted_candidates:
        if (
            _is_term_matched_with_boundary(term, core_lower)
            or _is_term_matched_with_boundary(term, q_lower)
        ):
            return prof

    return None


def resolve_mentioned_users(
    text: str,
    profiles: list[dict[str, Any]],
    *,
    exclude_user_id: int | None = None,
    exclude_names: set[str] | list[str] | None = None,
    allow_self: bool = False,
) -> list[dict[str, Any]]:
    """会話テキストから言及されているサーバーメンバー（現在名または過去名）を抽出・特定する。"""
    raw_text = _clean_text_for_search(text)
    if not raw_text.strip() or not profiles:
        return []

    lowered_text = raw_text.lower()
    hira_text = _to_hiragana(lowered_text)
    resolved: list[dict[str, Any]] = []
    stopwords = _get_mention_stopwords()
    seen_user_ids: set[int] = set()
    seen_display_names: set[str] = set()

    if exclude_user_id is not None and not allow_self:
        seen_user_ids.add(int(exclude_user_id))

    exclude_names_clean: set[str] = {
        str(n).strip().lower() for n in (exclude_names or []) if str(n).strip()
    }

    # 長い名前から優先的にマッチさせるためソート
    candidates: list[tuple[str, bool, dict[str, Any]]] = []
    for prof in profiles:
        uid = int(prof.get("user_id") or 0)
        if (uid in seen_user_ids and not allow_self) or uid == 0:
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

        # 単語境界または自立単語としてのマッチ確認
        # カタカナ単語の途中（例: ワンパース内のパー）や熟語の途中を誤検出しない
        matched = _is_term_matched_with_boundary(term, lowered_text)

        if matched:
            seen_user_ids.add(uid)
            seen_display_names.add(disp_name.lower())
            resolved.append({
                "user_id": uid,
                "current_display_name": disp_name,
                "current_name": str(prof.get("current_name") or ""),
                "matched_name": term,
                "is_alias": is_alias,
                "is_self": bool(exclude_user_id is not None and uid == int(exclude_user_id)),
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
    """解決されたメンバー情報をプロンプト向けテキストに整形する。

    トークン肥大化防止のため、エピソード記憶や関心事、人物像の長さをトリミングし、
    全体の文字数が OLLAMA_MENTIONED_USERS_MAX_CHARS（デフォルト1500文字）に収まるようバジェット管理する。
    """
    if not resolved_users:
        return []

    from lib.config_utils import cfg
    max_total_chars = int(cfg("OLLAMA_MENTIONED_USERS_MAX_CHARS", 1500) or 1500)

    lines: list[str] = [
        "【会話中で言及されたサーバーメンバー】",
    ]

    seen_disps: set[str] = set()
    for item in resolved_users[:3]:
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
        if len(party_impression) > 120:
            party_impression = party_impression[:117] + "..."

        rel_info = f"  - あなたとの関係性: 親愛度 {aff:+.1f}, 信頼度 {trust:+.1f}, 好意 {affection:+.1f}, 憎しみ {hatred:+.1f}"
        lines.append(rel_info)
        lines.append(f"  - あなたから見たこの人の人物像: {party_impression}")

        raw_thought = str(getattr(rel, "thought", "") or "").strip() if rel else ""
        if raw_thought and not is_meta_confusion_thought(raw_thought):
            thought_text = raw_thought[:60] if len(raw_thought) > 60 else raw_thought
            lines.append(f"  - あなたの印象（本音の一言）: 「{thought_text}」")

        # 関連する関心事情報（各最大40文字、最大3件）
        if user_interests:
            matched_interests = [
                str(ui.get("topic", "")).strip()[:40]
                for ui in user_interests
                if int(ui.get("user_id") or 0) == uid and str(ui.get("topic", "")).strip()
            ]
            if matched_interests:
                lines.append(f"  - この人の関心事・話題: {', '.join(matched_interests[:3])}")

        # 関連する記憶情報（各最大100文字、最大2件）
        if user_memories and uid in user_memories:
            mems = [m.strip()[:100] for m in user_memories[uid] if m.strip()]
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
        for item in resolved_users[:2]:
            curr_disp = item["current_display_name"]
            uid = item["user_id"]
            rel = relationships.get(uid) if relationships else None
            party_impression = describe_relationship_for_third_party(rel)
            if len(party_impression) > 100:
                party_impression = party_impression[:97] + "..."
            raw_thought = str(getattr(rel, "thought", "") or "").strip() if rel else ""
            thought_text = raw_thought[:50] if (raw_thought and not is_meta_confusion_thought(raw_thought)) else ""

            target_hint = f"対話相手（{current_user_name}さん）" if current_user_name else "対話相手"
            lines.append(f"※{target_hint}は、サーバーメンバー『{curr_disp}』さん（話題の第三者）が普段どんな人か、あなたの印象・感想を尋ねています。")
            lines.append(f"  - あなたから見た『{curr_disp}』さんの普段の人物像: {party_impression}")
            if thought_text:
                lines.append(f"  - あなたが普段感じている本音: 「{thought_text}」")

        lines.append("※【禁止事項】『見覚えがない』『知らない』『情報がない』『誰ですか？』『詳しく教えて』等と辞書的にシラを切ったり突き放したりすることは絶対に禁止です。『名前を聞かれても意図が掴めない』等の言い訳をしてはいけません。")
        lines.append("※上記の人物像や関係性（警戒している、仲良し、怖い等）、関連するエピソード記憶に基づいて、あなた自身の言葉で『その人に対する等身大の感想・気持ち』を自然に答えてください。")
    else:
        lines.append("※相手が上記メンバーについて質問したり感想・知っていることを求めている場合は、『情報がない』『知らない』と冷たく辞書的に突っぱねず、上記の関係性やあなたの印象、関連する記憶に基づいてあなたの言葉で自然に答えてください。")

    # 全体文字数バジェットチェック
    total_chars = sum(len(line) for line in lines)
    if total_chars > max_total_chars:
        trimmed_lines: list[str] = []
        current_len = 0
        for line in lines:
            # 過去話題や関心事は超過時に優先して落とす
            if ("この人の関心事・話題" in line or "この人に関する過去の話題・エピソード" in line) and current_len > max_total_chars // 2:
                continue
            if current_len + len(line) + 1 > max_total_chars:
                break
            trimmed_lines.append(line)
            current_len += len(line) + 1
        lines = trimmed_lines

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
    guild: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """サーバーメンバーの言及を解決し、(解決されたユーザー一覧, プロンプト用パーツ一覧) を返す。"""
    if guild_id is None and guild is None:
        return [], []

    try:
        profiles: list[dict[str, Any]] = []
        if guild_id is not None and memory_store and hasattr(memory_store, "list_user_profiles_for_guild"):
            profiles = await memory_store.list_user_profiles_for_guild(guild_id=int(guild_id), limit=1000)
        if guild is not None:
            profiles = merge_guild_and_db_profiles(profiles, guild)
        if not profiles:
            return [], []

        clean_ut = strip_discord_mentions(user_text).strip()
        search_target = clean_ut or user_text
        resolved_users = resolve_mentioned_users(
            search_target,
            profiles,
            exclude_user_id=exclude_user_id,
            exclude_names=exclude_names,
        )
        # 直接マッチ（単語・語幹）でのフォールバック
        if not resolved_users:
            direct_member = find_matching_guild_member(
                search_target,
                profiles,
                exclude_user_id=exclude_user_id,
                exclude_names=exclude_names,
            )
            if direct_member:
                uid = int(direct_member.get("user_id") or 0)
                disp_name = str(direct_member.get("current_display_name") or direct_member.get("current_name") or f"user_{uid}").strip()
                resolved_users = [{
                    "user_id": uid,
                    "current_display_name": disp_name,
                    "current_name": str(direct_member.get("current_name") or ""),
                    "matched_name": disp_name,
                    "is_alias": False,
                    "is_self": False,
                    "aliases": list(direct_member.get("aliases") or []),
                    "roles": list(direct_member.get("roles") or []),
                }]

        # 最新発言にメンバー名がなく、かつ照応表現（彼、その人、あいつ等）が含まれる場合のみ直前文脈を参照する
        if not resolved_users and context_lines and _ANAPHORA_PRONOUNS_PATTERN.search(search_target):
            context_snippet = " ".join(context_lines[-3:])
            resolved_users = resolve_mentioned_users(
                context_snippet,
                profiles,
                exclude_user_id=exclude_user_id,
                exclude_names=exclude_names,
            )

        if not resolved_users:
            return [], []

        persona = "default"
        if cfg_fn is not None and callable(cfg_fn):
            persona = str(cfg_fn("MEMORY_PERSONA_NAMESPACE", "default") or "default")

        effective_gid = int(guild_id if guild_id is not None else getattr(guild, "id", 0) or 0)
        user_interests_list = None
        if hasattr(memory_store, "list_user_interests") and effective_gid > 0:
            user_interests_list = await memory_store.list_user_interests(
                persona=persona,
                guild_id=effective_gid,
                limit=20,
            )

        # 該当メンバーに関するエピソード記憶の検索
        user_memories: dict[int, list[str]] = {}
        if hasattr(memory_store, "search_memories_by_keyword") and effective_gid > 0:
            for ru in resolved_users:
                ru_uid = ru["user_id"]
                ru_disp = ru["current_display_name"]
                try:
                    mem_results = await memory_store.search_memories_by_keyword(
                        ru_disp,
                        persona=persona,
                        guild_id=effective_gid,
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

