"""
[AI Agent Summary]
ユーザーIDとサーバーニックネーム（表示名）の紐付け・解決、エイリアス（旧ニックネーム変遷）の追跡、
および内部テキスト（タグ付き）からユーザー可視テキスト（最新サーバーニックネーム置換）への動的レンダリングを提供するモジュール。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("harness.agent.memory_manager.user_identity_resolver")

_UID_TAG_PATTERN = re.compile(r"\[uid:(\d+)\]")
_UID_PAREN_PATTERN = re.compile(r"[（(]ユーザーID:\s*(\d+)[）)]")


def extract_user_id_from_tag(text: str) -> int | None:
    """テキスト内に含まれる [uid:12345] または（ユーザーID: 12345）からユーザーIDを抽出する。"""
    if not text:
        return None
    m = _UID_TAG_PATTERN.search(text)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    m2 = _UID_PAREN_PATTERN.search(text)
    if m2:
        try:
            return int(m2.group(1))
        except (ValueError, TypeError):
            pass
    return None


def format_user_internal_tag(user_id: int | str) -> str:
    """内部記録用のユーザーIDタグ [uid:12345] を生成する。"""
    return f"[uid:{user_id}]"


def strip_user_internal_tags(text: str) -> str:
    """テキストから内部タグ [uid:12345] を除去し、余分なスペースを整える。"""
    if not text:
        return ""
    cleaned = _UID_TAG_PATTERN.sub("", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


def replace_aliases_with_current_name(
    text: str,
    *,
    current_name: str,
    aliases: list[str],
) -> str:
    """
    テキスト内に含まれる旧ニックネーム（aliases）を最新のサーバーニックネーム（current_name）に置換する。
    
    例: aliases=['でゅねいンマン'], current_name='超でゅねいンマン'
        '『でゅねいンマン』さんはワスが好き' -> '『超でゅねいンマン』さんはワスが好き'
    """
    if not text or not current_name:
        return text or ""

    clean_current = str(current_name).strip()
    if clean_current.startswith(("『", "「")) and clean_current.endswith(("』", "」")):
        clean_current = clean_current[1:-1].strip()

    token = "\x00CURRENT_NAME_TOKEN\x00"
    result = text.replace(clean_current, token)

    # 長いエイリアスから順に置換して部分一致誤爆を防ぐ
    sorted_aliases = sorted(
        {str(a).strip() for a in aliases if str(a).strip() and str(a).strip() != clean_current},
        key=len,
        reverse=True,
    )

    for alias in sorted_aliases:
        # 括弧付きのエイリアスを優先して最新名に置換
        # 例: 『alias』さん -> 『current』さん
        for open_b, close_b in (("『", "』"), ("「", "」")):
            bracket_alias = f"{open_b}{alias}{close_b}"
            bracket_current = f"{open_b}{token}{close_b}"
            if bracket_alias in result:
                result = result.replace(bracket_alias, bracket_current)

        # 敬称付きエイリアス
        # 例: aliasさん -> currentさん
        for honorific in ("さん", "くん", "君", "ちゃん", "氏", "様"):
            hon_alias = f"{alias}{honorific}"
            hon_current = f"{token}{honorific}"
            if hon_alias in result:
                result = result.replace(hon_alias, hon_current)

        # 単体出現
        if alias in result:
            result = result.replace(alias, token)

    # プレースホルダーを最新名に戻す
    result = result.replace(token, clean_current)
    return result


def render_user_facing_text(
    text: str,
    *,
    target_user_id: int | str | None = None,
    current_display_name: str | None = None,
    aliases: list[str] | None = None,
) -> str:
    """
    内部処理用テキスト（[uid:12345] タグや過去のエイリアスを含む可能性のあるテキスト）を、
    ユーザーやプロンプトから見える形式（タグ除去＆最新サーバーニックネーム置換）に整形する。
    """
    if not text:
        return ""

    result = text

    # 1. 内部タグ [uid:...] のユーザー名への置換または除去
    if target_user_id is not None and current_display_name:
        clean_disp = str(current_display_name).strip()
        if clean_disp.startswith(("『", "「")) and clean_disp.endswith(("』", "」")):
            clean_disp = clean_disp[1:-1].strip()
        # [uid:12345] 『旧名』さん -> 『最新名』さん のようにまとめる
        tag = format_user_internal_tag(target_user_id)
        if tag in result:
            result = result.replace(tag, "").strip()

    # 残った uid タグを除去
    result = strip_user_internal_tags(result)

    # 2. 過去のエイリアス（旧ニックネーム）を最新サーバーニックネームに置換
    if current_display_name and aliases:
        result = replace_aliases_with_current_name(
            result,
            current_name=current_display_name,
            aliases=aliases,
        )

    return result


async def resolve_user_display_name(
    user_id: int,
    *,
    guild: Any = None,
    memory_store: Any = None,
    default_name: str | None = None,
) -> str:
    """
    ユーザーIDから現在の最新サーバーニックネーム（表示名）を動的に解決する。
    優先順位:
    1. Guild.get_member(user_id) の display_name（リアルタイム最新値）
    2. memory_store.get_user_profile(...) の current_display_name
    3. default_name（渡されている場合）
    4. フォールバック "ユーザー_{user_id}"
    """
    if guild is not None and hasattr(guild, "get_member"):
        try:
            member = guild.get_member(user_id)
            if member is not None:
                disp = str(getattr(member, "display_name", "") or "").strip()
                if disp:
                    return disp
        except Exception as e:
            log.debug("guild.get_member failed for %s: %r", user_id, e)

    if memory_store is not None and hasattr(memory_store, "get_user_profile"):
        try:
            gid = int(getattr(guild, "id", 0) or 0) if guild is not None else 0
            profile = await memory_store.get_user_profile(guild_id=gid, user_id=user_id)
            if profile and profile.get("current_display_name"):
                disp = str(profile["current_display_name"]).strip()
                if disp:
                    return disp
        except Exception as e:
            log.debug("memory_store.get_user_profile failed for %s: %r", user_id, e)

    if default_name and str(default_name).strip():
        return str(default_name).strip()

    return f"ユーザー_{user_id}"


async def get_user_aliases_from_store(
    user_id: int,
    *,
    guild: Any = None,
    memory_store: Any = None,
) -> list[str]:
    """
    MemoryStore の user_profiles から該当ユーザーの過去のエイリアス（旧ニックネーム・旧アカウント名等）を取得する。
    """
    aliases: list[str] = []
    if memory_store is not None and hasattr(memory_store, "get_user_profile"):
        try:
            gid = int(getattr(guild, "id", 0) or 0) if guild is not None else 0
            profile = await memory_store.get_user_profile(guild_id=gid, user_id=user_id)
            if profile:
                raw_aliases = profile.get("aliases") or []
                for a in raw_aliases:
                    clean_a = str(a).strip()
                    if clean_a and clean_a not in aliases:
                        aliases.append(clean_a)
                c_name = str(profile.get("current_name") or "").strip()
                if c_name and c_name not in aliases:
                    aliases.append(c_name)
        except Exception as e:
            log.debug("get_user_aliases_from_store failed for %s: %r", user_id, e)
    return aliases
