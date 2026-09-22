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
