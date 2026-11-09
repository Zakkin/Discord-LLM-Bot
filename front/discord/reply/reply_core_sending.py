"""OllamaChatReplyCoreMixinの返信送信・分割送信・絵文字相槌・直接返信判定を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from typing import Any, TYPE_CHECKING

import discord

from lib.config_utils import cfg_persona_namespace
from ollama_bot.common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    sanitize_generated_reply,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import (
    channel_id,
    display_name,
    recent_context_items,
)
from front.discord.reply.classifier import decide_reply_mode, is_stale_reply
from harness.agent.prompt_parser.parser.reply_helpers import _format_reply
from front.discord.reply.reply_core_common import (
    _AIZUCHI_PATTERN,
    _OllamaChatReplyCoreBase,
    _split_reply_text,
    ReplyTargetDeletedError,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    log,
)


class ReplySendingMixin(_OllamaChatReplyCoreBase):
    """返信メッセージの送信、分割送信、直接リプライ/チャンネル投稿の判定、相槌リアクション化を担当するMixin。"""

    async def _store_bot_utterance(self, message: discord.Message, raw_reply: str) -> None:
        try:
            cleaned_reply = extract_first_user_facing_reply(raw_reply)
            if cleaned_reply and not looks_like_abnormal_assistant_reply(cleaned_reply):
                guild_obj = getattr(message, "guild", None)
                channel_obj = getattr(message, "channel", None)
                await self.memory_store.add_bot_utterance(
                    persona=cfg_persona_namespace(custom_cfg=cfg),
                    guild_id=getattr(guild_obj, "id", None),
                    channel_id=getattr(channel_obj, "id", None),
                    content=cleaned_reply,
                )
        except Exception as e:
            log.exception("bot utterance save failed: %s", e)

    def _should_use_direct_reply(
        self,
        message: discord.Message,
        *,
        is_direct: bool = False,
        reply: str = "",
    ) -> bool:
        if cfg_bool("ALWAYS_CHANNEL_SEND", False):
            return False
        if cfg_bool("ALWAYS_DIRECT_REPLY", False):
            return True
        if is_direct and not cfg_bool("DIRECT_REPLY_ON_MENTION", True):
            return False

        # 後方互換・強制返信フラグ: DIRECT_REPLY_RATE >= 1.0 の場合は即座に True
        rate = cfg_float("DIRECT_REPLY_RATE", 0.0)
        if rate >= 1.0:
            return True

        cid = channel_id(message) if message is not None else None
        cache = getattr(self, "channel_context_cache", None)
        queue = cache.get(cid) if cache and cid is not None else None
        tracker = getattr(self, "thread_tracker", None)
        return decide_reply_mode(
            target_message=message,
            reply_text=reply,
            is_direct=is_direct,
            context_queue=queue,
            thread_tracker=tracker,
        )

    def _is_stale_reply(
        self,
        message: discord.Message | None,
        *,
        is_direct: bool = False,
    ) -> bool:
        if message is None:
            return False
        queue = self._get_channel_context_queue(message)
        bot_user_id = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        return is_stale_reply(
            message,
            is_direct=is_direct,
            context_queue=queue,
            bot_user_id=bot_user_id,
        )

    async def _reply_to_message(
        self,
        target_message: discord.Message,
        reply: str,
        *,
        mention_author: bool = False,
        is_direct: bool = False,
        drop_if_target_deleted: bool = False,
    ) -> discord.Message:
        raw_reply = str(reply or "").strip()
        payload = _format_reply(reply)
        if self._should_use_direct_reply(target_message, reply=raw_reply, is_direct=is_direct):
            try:
                sent = await target_message.reply(payload, mention_author=mention_author)
            except discord.NotFound:
                if drop_if_target_deleted:
                    log.info(
                        "reply target deleted; dropping non-direct reply (message_id=%s)",
                        getattr(target_message, "id", None),
                    )
                    raise ReplyTargetDeletedError(str(getattr(target_message, "id", "")))
                log.info("reply target disappeared; falling back to channel send (message_id=%s)", getattr(target_message, "id", None))
                sent = await target_message.channel.send(payload)
            except discord.Forbidden as e:
                # 160002: Cannot reply without permission to read message history
                # 50013: Missing Permissions (general send/reply permission)
                err_code = getattr(e, "code", None)
                if err_code in (160002, 50013):
                    log.warning(
                        "reply forbidden (code=%s); falling back to channel send (message_id=%s channel=%s)",
                        err_code,
                        getattr(target_message, "id", None),
                        getattr(getattr(target_message, "channel", None), "id", None),
                    )
                    sent = await target_message.channel.send(payload)
                else:
                    raise
            except discord.HTTPException as e:
                if getattr(e, "code", None) == 50035 and "Unknown message" in str(e):
                    if drop_if_target_deleted:
                        log.info(
                            "reply target deleted; dropping non-direct reply (message_id=%s)",
                            getattr(target_message, "id", None),
                        )
                        raise ReplyTargetDeletedError(str(getattr(target_message, "id", "")))
                    log.info(
                        "reply target unknown; falling back to channel send (message_id=%s, code=%s)",
                        getattr(target_message, "id", None),
                        getattr(e, "code", None),
                    )
                    sent = await target_message.channel.send(payload)
                else:
                    raise
        else:
            sent = await target_message.channel.send(payload)
        return sent

    def _resolve_send_other_names(
        self,
        message: discord.Message | None,
        target_name: str,
    ) -> set[str]:
        """送信直前にサニタイズ対象とする他者名（直近会話話者、返信元作成者、メンション対象等）を収集する。"""
        other_names: set[str] = set()
        clean_target = (target_name or "").strip()
        if message is None:
            return other_names

        cid = getattr(getattr(message, "channel", None), "id", None)
        # 1. チャンネル直近キャッシュから取得
        if cid:
            cached_items = recent_context_items(
                self.channel_context_cache,
                cid,
                before_message=message,
                max_age_sec=3600,
            )
            for item in cached_items:
                line = str(item.get("line") or "")
                line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
                if ":" in line_no_ts:
                    speaker_part = line_no_ts.split(":", 1)[0].strip()
                    speaker = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
                    if speaker and speaker != clean_target:
                        other_names.add(speaker)

        # 2. 返信元の親メッセージ（reference）の作成者名を無条件で追加
        ref = getattr(message, "reference", None)
        if ref:
            ref_msg = getattr(ref, "resolved", None) or getattr(ref, "cached_message", None)
            if ref_msg is not None:
                ref_author = display_name(ref_msg).strip()
                if ref_author and ref_author != clean_target:
                    other_names.add(ref_author)
                ref_raw_author = getattr(getattr(ref_msg, "author", None), "name", "")
                if ref_raw_author and ref_raw_author != clean_target:
                    other_names.add(ref_raw_author)
            mid = getattr(ref, "message_id", None)
            if mid and cid:
                q = self.channel_context_cache.get(cid)
                if q:
                    for itm in q:
                        if itm.get("id") == mid:
                            line = str(itm.get("line") or "")
                            line_no_ts = re.sub(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*", "", line.strip())
                            if ":" in line_no_ts:
                                speaker_part = line_no_ts.split(":", 1)[0].strip()
                                spk = re.sub(r"\s*(?:\([^)]*\)|\[[^\]]*\])\s*$", "", speaker_part).strip()
                                if spk and spk != clean_target:
                                    other_names.add(spk)

        # 3. メンションされているユーザーの名前を追加
        for m in getattr(message, "mentions", []) or []:
            m_name = display_name(m).strip()
            if m_name and m_name != clean_target:
                other_names.add(m_name)
            raw_name = getattr(m, "name", "")
            if raw_name and raw_name != clean_target:
                other_names.add(raw_name)

        # 4. スレッド作成者等の追跡
        ch = getattr(message, "channel", None)
        if ch is not None:
            thread_owner = getattr(ch, "owner", None)
            if thread_owner is not None:
                to_name = display_name(thread_owner).strip()
                if to_name and to_name != clean_target:
                    other_names.add(to_name)

        # 5. 発言者本人の生アカウント名（サーバーニックネームと乖離している場合の多重防御）
        author = getattr(message, "author", None)
        if author is not None:
            raw_author = getattr(author, "name", "")
            if raw_author and raw_author != clean_target:
                other_names.add(raw_author)

        return other_names

    def _resolve_aizuchi_emoji(self, text: str) -> str:
        """相槌テキストに応じた絵文字を決定する。"""
        if any(m in text for m in ("わはは", "あはは", "草", "笑")):
            return "😆"
        if any(m in text for m in ("すごい", "すごーい", "いいね")):
            return "✨"
        if any(m in text for m in ("了解", "おっけー", "オッケー", "はい")):
            return "🙆"
        if any(m in text for m in ("おつかれ", "お疲れ", "感謝", "ありがとう")):
            return "🙏"
        return "👍"

    async def _send_reply(
        self,
        message: discord.Message,
        reply: str,
        *,
        is_direct: bool = False,
    ) -> discord.Message | None:
        if self._is_stale_reply(message, is_direct=is_direct):
            elapsed_sec = self._calculate_message_elapsed_sec(message)
            gap = self._calculate_channel_gap(message)
            log.info(
                "TRACE stale reply dropped: channel=%s message_id=%s author=%s elapsed_sec=%.1f gap=%d (conversation already moved forward)",
                getattr(message, "channel", None),
                getattr(message, "id", None),
                getattr(message, "author", None),
                elapsed_sec,
                gap,
            )
            return None

        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(message) if message is not None else "").strip()
        other_names = self._resolve_send_other_names(message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(message, "content", "") or ""),
        )

        # ステップ3: 相槌のリアクション化
        # 返信文が12文字以内の相槌パターンであり、かつ random.random() < REPLY_REACTION_ONLY_RATE の場合、
        # 文章送信を行わず _safe_add_reaction で感情に応じた絵文字を付与して終了する（メンション返信時は除外）
        is_aizuchi = (
            not is_direct
            and len(raw_reply) <= 12
            and bool(_AIZUCHI_PATTERN.match(raw_reply))
        )
        if is_aizuchi:
            reaction_only_rate = cfg_float("REPLY_REACTION_ONLY_RATE", 0.0)
            if reaction_only_rate > 0.0 and random.random() < reaction_only_rate:
                emoji = self._resolve_aizuchi_emoji(raw_reply)
                safe_add = getattr(self, "_safe_add_reaction", None)
                if callable(safe_add):
                    await safe_add(message, emoji)
                elif hasattr(message, "add_reaction"):
                    with contextlib.suppress(Exception):
                        await message.add_reaction(emoji)
                log.info(
                    "TRACE reply converted to reaction: channel=%s message_id=%s reply=%r emoji=%s",
                    getattr(message, "channel", None),
                    getattr(message, "id", None),
                    raw_reply,
                    emoji,
                )
                return None

        # ステップ2: タイピング表示付き分割送信の実装
        split_enabled = cfg_bool("REPLY_SPLIT_ENABLED", False)
        max_chunks = cfg_int("REPLY_SPLIT_MAX_CHUNKS", 3)
        chunks = _split_reply_text(raw_reply, max_chunks=max_chunks) if split_enabled else [raw_reply]

        if not chunks:
            chunks = [raw_reply]

        # 1. 先頭チャンク: 既存ロジック（直接リプライ要否に従う）で送信
        first_chunk = chunks[0]
        try:
            sent = await self._reply_to_message(
                message,
                first_chunk,
                mention_author=False,
                is_direct=is_direct,
                drop_if_target_deleted=not is_direct,
            )
        except ReplyTargetDeletedError:
            return None

        log.info(
            "TRACE sent reply: channel=%s target_author=%s sent_id=%s content=%r",
            getattr(message, "channel", None),
            getattr(message, "author", None),
            getattr(sent, "id", None),
            first_chunk,
        )

        # 2. 2つ目以降のチャンク: タイピング表示付き送信
        if len(chunks) > 1:
            channel = getattr(message, "channel", None)
            sec_per_char = cfg_float("REPLY_TYPING_SEC_PER_CHAR", 0.05)
            remaining_chunks = list(chunks[1:])

            for idx, chunk in enumerate(remaining_chunks, start=2):
                typing_delay = max(0.8, min(3.0, len(chunk) * sec_per_char))
                try:
                    if channel is not None and hasattr(channel, "typing"):
                        async with channel.typing():
                            await asyncio.sleep(typing_delay)
                    else:
                        await asyncio.sleep(typing_delay)

                    if channel is not None and hasattr(channel, "send"):
                        await channel.send(chunk)
                        log.info(
                            "TRACE sent reply chunk: channel=%s chunk_idx=%d/%d content=%r",
                            channel,
                            idx,
                            len(chunks),
                            chunk,
                        )
                except Exception as e:
                    log.warning("failed to send chunk %d/%d; attempting fallback combine send: %r", idx, len(chunks), e)
                    # 3. 送信例外時は残りのチャンクを1つにまとめて即時送信するフォールバック
                    current_remaining = [chunk] + remaining_chunks[idx - 1 :]
                    combined = " ".join(current_remaining)
                    if channel is not None and hasattr(channel, "send"):
                        try:
                            await channel.send(combined)
                            log.info("TRACE sent fallback combined chunk: channel=%s content=%r", channel, combined)
                        except Exception as fb_err:
                            log.error("failed to send combined fallback chunks: %r", fb_err)
                    break

        await self._store_bot_utterance(message, raw_reply)
        return sent

    async def _send_provisional_reply(
        self,
        message: discord.Message,
        reply: str,
        *,
        is_direct: bool = False,
    ) -> discord.Message:
        return await self._reply_to_message(message, reply, mention_author=False, is_direct=is_direct)

    async def _send_followup_reply(
        self,
        parent_message: discord.Message,
        reply: str,
        *,
        is_direct: bool = True,
    ) -> discord.Message:
        bot_uid = self.bot.user.id if getattr(getattr(self, "bot", None), "user", None) else None
        target_name = (display_name(parent_message) if parent_message is not None else "").strip()
        other_names = self._resolve_send_other_names(parent_message, target_name)
        raw_reply = sanitize_generated_reply(
            str(reply or "").strip(),
            bot_user_id=bot_uid,
            replacement_name=target_name,
            target_name=target_name,
            other_names=other_names,
            user_text=str(getattr(parent_message, "content", "") or ""),
        )
        sent = await self._reply_to_message(parent_message, raw_reply, mention_author=False, is_direct=is_direct)
        log.info(
            "TRACE sent reply: channel=%s target_author=%s sent_id=%s content=%r",
            getattr(parent_message, "channel", None),
            getattr(parent_message, "author", None),
            getattr(sent, "id", None),
            raw_reply,
        )
        await self._store_bot_utterance(parent_message, raw_reply)
        return sent
