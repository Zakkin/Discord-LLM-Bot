"""Bot自身のDiscordアカウント情報・表示名・キャラクター呼称を集約・認識するためのモジュール。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lib.config_utils import cfg


@dataclass(frozen=True)
class BotIdentity:
    """Botの同一性（名前・ニックネーム・キャラクター呼称）を保持するイミュータブルなデータクラス。"""
    user_id: int | None = None
    username: str = ""
    global_display_name: str = ""
    guild_display_name: str = ""
    configured_name: str = ""
    first_person: str = ""
    character_names: list[str] = field(default_factory=list)
    all_names: list[str] = field(default_factory=list)

    @property
    def primary_name(self) -> str:
        """プロンプト等で使用する代表名。character_namesがあれば先頭の固有名、なければconfigured_name等。"""
        if self.character_names:
            for n in self.character_names:
                if n.lower() not in ("ai", "bot", "ボット", "アシスタント"):
                    return n
            return self.character_names[0]
        if self.configured_name:
            return self.configured_name
        return self.guild_display_name or self.global_display_name or self.username or "AI"

    def is_mentioned_in(self, text: str) -> bool:
        """テキスト内にBotの名前・呼称が含まれているかを判定する。"""
        return is_bot_name_mentioned(text, self)


def _extract_character_names_from_config() -> list[str]:
    """設定値（システムプロンプトや名前設定）からキャラクター名候補を動的に抽出する。"""
    names: list[str] = []

    # 1. BOT_ALIASES 設定を最優先
    raw_aliases = cfg("BOT_ALIASES", ())
    if isinstance(raw_aliases, (list, tuple, set)):
        for a in raw_aliases:
            val = str(a).strip()
            if val and val not in names:
                names.append(val)
    elif isinstance(raw_aliases, str) and raw_aliases.strip():
        for a in raw_aliases.split(","):
            val = a.strip()
            if val and val not in names:
                names.append(val)

    # 2. 一人称設定
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
    if first_person and first_person not in names:
        names.append(first_person)

    # 3. 表示名
    configured = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if configured and configured not in names:
        names.append(configured)
        cleaned = re.sub(r"^(?:AI|bot|ボット)\s*", "", configured, flags=re.IGNORECASE).strip()
        if cleaned and cleaned not in names:
            names.append(cleaned)

    # 4. フォールバック（設定値がない場合はシステムプロンプトから動的抽出）
    if not names:
        system_prompt = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
        if system_prompt:
            match = re.search(r"あなたの名前は([^。\n]+)です", system_prompt)
            if match:
                raw_name = match.group(1).strip()
                paren_match = re.search(r"[（\(]([^）\)]+)[）\)]", raw_name)
                if paren_match:
                    subname = paren_match.group(1).strip()
                    if subname and subname not in names:
                        names.append(subname)
                main_name = re.sub(r"[（\(].*?[）\)]", "", raw_name).strip()
                if main_name and main_name not in names:
                    names.append(main_name)

    generic = ["AI", "Bot", "bot", "ボット", "アシスタント"]
    for g in generic:
        if g not in names:
            names.append(g)

    return [n for n in names if n]


def resolve_bot_identity(bot: Any = None, message: Any = None) -> BotIdentity:
    """
    Discord BotインスタンスおよびメッセージコンテキストからBotIdentityを解決・生成する。
    bot や message が None の場合でも設定値から安全にフォールバック生成する。
    """
    user_id: int | None = None
    username: str = ""
    global_display_name: str = ""
    guild_display_name: str = ""

    bot_user = getattr(bot, "user", None)
    if bot_user is not None:
        user_id = getattr(bot_user, "id", None)
        username = str(getattr(bot_user, "name", "") or "").strip()
        global_display_name = str(getattr(bot_user, "display_name", "") or "").strip()

    guild = getattr(message, "guild", None)
    if guild is not None and hasattr(guild, "me") and guild.me is not None:
        guild_display_name = str(getattr(guild.me, "display_name", "") or "").strip()
        nick = str(getattr(guild.me, "nick", "") or "").strip()
        if nick and nick != guild_display_name:
            guild_display_name = nick
    elif bot is not None and hasattr(bot, "guilds"):
        for g in getattr(bot, "guilds", []):
            me = getattr(g, "me", None)
            if me is not None:
                disp = str(getattr(me, "display_name", "") or "").strip()
                if disp:
                    guild_display_name = disp
                    break

    configured_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    char_names = _extract_character_names_from_config()

    all_names_set: set[str] = set()
    for n in [username, global_display_name, guild_display_name, configured_name] + char_names:
        cleaned = str(n or "").strip()
        if cleaned:
            all_names_set.add(cleaned)
            no_ai = re.sub(r"^(?:AI|bot|ボット)\s*", "", cleaned, flags=re.IGNORECASE).strip()
            if len(no_ai) >= 2:
                all_names_set.add(no_ai)

    sorted_names = sorted(all_names_set, key=lambda s: len(s), reverse=True)
    first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip() or "私"

    return BotIdentity(
        user_id=user_id,
        username=username,
        global_display_name=global_display_name,
        guild_display_name=guild_display_name,
        configured_name=configured_name,
        first_person=first_person,
        character_names=char_names,
        all_names=sorted_names,
    )


def is_bot_name_mentioned(text: str, identity: BotIdentity | None = None) -> bool:
    """テキスト内にBotの名前・呼称が含まれているかを判定する。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if identity is None:
        identity = resolve_bot_identity()

    if identity.user_id is not None:
        if f"<@{identity.user_id}>" in raw or f"<@!{identity.user_id}>" in raw:
            return True

    lowered = raw.lower()
    for name in identity.all_names:
        if not name:
            continue
        if len(name) <= 2:
            pattern = rf"(?:^|[\s@「『(（]|\b){re.escape(name.lower())}(?:$|[\s@」』)）はがのにへとって]|さん|ちゃん|君|くん|\b)"
            if re.search(pattern, lowered):
                return True
        else:
            if name.lower() in lowered:
                return True
    return False


def format_bot_names_for_prompt(identity: BotIdentity | None = None) -> str:
    """事前分析や返答プロンプト向けに、Botの名前・呼称一覧をフォーマットした文字列を返す。"""
    if identity is None:
        identity = resolve_bot_identity()

    primary_name = identity.guild_display_name or identity.configured_name or identity.global_display_name or "AI"
    key_aliases = [n for n in identity.all_names if n and n != primary_name and len(n) >= 2][:6]
    aliases_str = "、".join(f"『{a}』" for a in key_aliases) if key_aliases else "『AI』『bot』"
    return f"『{primary_name}』（別名・呼称: {aliases_str}）"


def get_bot_identifiers(bot: Any = None, message: Any = None) -> tuple[set[str], set[str]]:
    """自Botの識別名一覧と、他Botの既知の識別名一覧を返す。"""
    import os

    config_name = str(os.environ.get("OLLAMA_BOT_CONFIG", "")).split(".")[-1].lower()
    clean_cfg = config_name[7:] if config_name.startswith("config_") else config_name

    identity = resolve_bot_identity(bot, message)
    self_identifiers = {
        config_name,
        clean_cfg,
        identity.configured_name.lower(),
        identity.guild_display_name.lower(),
        identity.username.lower(),
    }
    for name in identity.all_names:
        cleaned_n = name.lower().strip()
        if cleaned_n and cleaned_n not in ("ai", "bot", "ボット", "アシスタント"):
            self_identifiers.add(cleaned_n)
    self_identifiers.discard("")

    all_available_configs: list[str] = []
    try:
        from ..config_loader import find_available_config_modules
        all_available_configs = [c.lower() for c in find_available_config_modules()]
    except Exception:
        pass

    other_configs = set()
    for c in all_available_configs:
        c_clean = c[7:] if c.startswith("config_") else c
        if c != config_name and c_clean != clean_cfg:
            other_configs.add(c)
            other_configs.add(c_clean)

    return self_identifiers, other_configs


def is_target_of_command(
    bot: Any,
    message: Any,
    tokens: list[str] | None = None,
    is_admin_command: bool = False,
) -> bool:
    """メッセージがこのBotインスタンスを対象としたコマンドであるかを判定する。

    複数Botが同一サーバーで並行稼働している環境において、
    プレフィックスコマンドやテキストコマンドがBotの数だけ重複実行されるのを防ぐ。
    """
    import os
    import discord

    # 1. 稼働状態（スタンバイ状態）のチェック
    # adminコマンド(!bot start 等)は停止中も受け付けるが、通常コマンドは停止中なら拒否
    if not is_admin_command:
        get_cog_fn = getattr(bot, "get_cog", None)
        if callable(get_cog_fn):
            cog = get_cog_fn("OllamaChatCog")
            if cog is not None and not getattr(cog, "is_active", True):
                return False

    # 2. メンション判定
    bot_user = getattr(bot, "user", None)
    bot_user_id = getattr(bot_user, "id", None)
    mentions = getattr(message, "mentions", []) or []
    bot_mentions = [u for u in mentions if getattr(u, "bot", False) or (bot_user_id is not None and getattr(u, "id", None) == bot_user_id)]
    if bot_mentions and bot_user_id is not None:
        # メッセージにBotへのメンションが含まれている場合、自分が含まれていればTrue、他BotのみならFalse
        return any(getattr(u, "id", None) == bot_user_id for u in bot_mentions)

    # 3. 返信ツリー判定（メッセージがBotへの返信である場合）
    ref = getattr(message, "reference", None)
    if ref is not None and bot_user_id is not None:
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and isinstance(resolved, discord.Message):
            ref_author = getattr(resolved, "author", None)
            if ref_author is not None and (getattr(ref_author, "bot", False) or getattr(ref_author, "id", None) == bot_user_id):
                return getattr(ref_author, "id", None) == bot_user_id

    # 4. コマンド引数に対象Botの明示的指定（名前、エイリアス、コンフィグ名、all等）があるか
    if tokens is None:
        text = str(getattr(message, "content", "") or "").strip()
        tokens = text.split()

    extra_args = [p.lower() for p in tokens[1:]] if len(tokens) > 1 else []
    if "all" in extra_args or "全員" in extra_args or "全部" in extra_args:
        return True

    self_identifiers, other_configs = get_bot_identifiers(bot, message)

    for arg in extra_args:
        if arg in self_identifiers or any(ident in arg for ident in self_identifiers if len(ident) >= 2):
            return True

    for arg in extra_args:
        if arg in other_configs:
            return False

    # 5. 引数にもメンションにも対象指定がない場合
    # 自Botのプライマリチャンネルでのコマンドであれば、自Botが応答
    channel_obj = getattr(message, "channel", None)
    chan_id = getattr(channel_obj, "id", None) if channel_obj is not None else None
    primary_cid = int(cfg("OLLAMA_CHANNEL_ID", 0) or 0)
    if primary_cid and chan_id is not None and chan_id == primary_cid:
        return True

    all_available_configs: list[str] = []
    try:
        from ..config_loader import find_available_config_modules
        all_available_configs = [c.lower() for c in find_available_config_modules()]
    except Exception:
        pass

    config_name = str(os.environ.get("OLLAMA_BOT_CONFIG", "")).split(".")[-1].lower()
    clean_cfg = config_name[7:] if config_name.startswith("config_") else config_name
    # 共通チャンネル等で宛先指定がない場合：代表Bot（config_img 等）のみが代表応答
    # ※テスト用設定(config_test等)や単体環境では常にTrue
    if all_available_configs and (config_name in all_available_configs or f"config_{clean_cfg}" in all_available_configs):
        primary_config = "config_img" if "config_img" in all_available_configs else all_available_configs[0]
        return config_name == primary_config or clean_cfg == primary_config or f"config_{clean_cfg}" == primary_config

    return True


def get_known_other_bot_names(bot: Any = None, message: Any = None) -> set[str]:
    """他Botの既知の名前・呼称（小文字）のセットを収集して返す。"""
    bot_user = getattr(bot, "user", None)
    bot_user_id = getattr(bot_user, "id", None) if bot_user is not None else None

    other_names: set[str] = set()

    # 1. コンフィグ識別名から収集
    try:
        _, other_configs = get_bot_identifiers(bot, message)
        for c in other_configs:
            cleaned = c.strip().lower()
            if len(cleaned) >= 2 and cleaned not in ("ai", "bot", "ボット", "アシスタント"):
                other_names.add(cleaned)
    except Exception:
        pass

    # 2. サーバー内のBotメンバーから収集
    guild = getattr(message, "guild", None)
    if guild is None and bot is not None and hasattr(bot, "guilds"):
        guilds = getattr(bot, "guilds", [])
        if guilds:
            guild = guilds[0]

    if guild is not None and hasattr(guild, "members"):
        for member in getattr(guild, "members", []):
            if getattr(member, "bot", False):
                m_id = getattr(member, "id", None)
                if bot_user_id is not None and m_id == bot_user_id:
                    continue
                for name_attr in ("name", "display_name", "nick", "global_name"):
                    raw_val = getattr(member, name_attr, None)
                    if raw_val:
                        val = str(raw_val).strip().lower()
                        if len(val) >= 2 and val not in ("ai", "bot", "ボット", "アシスタント"):
                            other_names.add(val)
                            cleaned_ai = re.sub(r"^(?:ai|bot|ボット)\s*", "", val, flags=re.IGNORECASE).strip()
                            if len(cleaned_ai) >= 2:
                                other_names.add(cleaned_ai)

    return other_names


def is_message_addressed_to_other_bot(
    message: Any,
    bot_user_id: int | None = None,
    bot: Any = None,
) -> bool:
    """メッセージが自Botではなく『他のBot宛て』に送られたものであるかを判定する。

    複数Botが同一サーバー・同一チャンネルに同居している環境において、
    他Bot宛てのメンション、他Botへの返信、他Botへの呼びかけを自Botが誤って取り込み、
    感情値・感情理由の書き換えや不要な割り込み返信を行うのを防ぐ。
    """
    import discord

    if bot_user_id is None and bot is not None:
        bot_user = getattr(bot, "user", None)
        if bot_user is not None:
            bot_user_id = getattr(bot_user, "id", None)

    # 1. メンション判定
    mentions = getattr(message, "mentions", []) or []
    bot_mentions = [
        u for u in mentions
        if getattr(u, "bot", False) or (bot_user_id is not None and getattr(u, "id", None) == bot_user_id)
    ]
    if bot_mentions:
        # メッセージに自Botへのメンションが含まれている場合は自Bot宛てでもある
        if bot_user_id is not None and any(getattr(u, "id", None) == bot_user_id for u in bot_mentions):
            return False
        # 自Botが含まれておらず、他のBotへのメンションが含まれている場合は他Bot宛て
        other_bot_mentions = [
            u for u in bot_mentions
            if getattr(u, "bot", False) and (bot_user_id is None or getattr(u, "id", None) != bot_user_id)
        ]
        if other_bot_mentions:
            return True

    # 2. 返信ツリー（Message Reference）判定
    ref = getattr(message, "reference", None)
    if ref is not None:
        resolved = getattr(ref, "resolved", None)
        if resolved is not None and isinstance(resolved, discord.Message):
            ref_author = getattr(resolved, "author", None)
            if ref_author is not None and getattr(ref_author, "bot", False):
                # 返信先がBotである場合
                if bot_user_id is not None and getattr(ref_author, "id", None) == bot_user_id:
                    return False  # 自Botへの返信
                from .discord_helpers import has_anon_buttons
                if has_anon_buttons(resolved):
                    return False  # 匿名Botによる投稿への返信は他Bot宛てとみなさない
                # 自Bot以外のBotへの返信
                return True

    # 3. テキスト先頭での呼びかけ判定
    raw_content = str(getattr(message, "content", "") or "").strip()
    if raw_content:
        identity = resolve_bot_identity(bot, message)
        # 自Botの名前がメッセージに含まれている場合は他Bot宛てのみとはみなさない
        if identity.is_mentioned_in(raw_content):
            return False

        other_names = get_known_other_bot_names(bot, message)
        if other_names:
            lowered = raw_content.lower()
            # 文頭の呼びかけパターン（例: "シャンクス、", "test-server: ", "@koito "）
            match = re.match(r"^[@＠]?([a-zA-Z0-9_\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\-]+)[,、:：\s]", lowered)
            if match:
                called_name = match.group(1).strip()
                if called_name in other_names:
                    return True

    return False

