"""「要約」「わかりやすい解説」「これ何？」「ファクトチェック」のコンテキストメニュー処理と実行フローを担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import re
from typing import Optional, Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from lib.discord_utils import clean_user_input_text

from ..ollama_chat_helpers import *
from ..ollama_chat_helpers import (
    _build_umigame_tool_block_text,
    _is_umigame_active_channel,
)

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _OllamaChatInvestigateBase = OllamaChatProtocol
else:
    _OllamaChatInvestigateBase = object


class OllamaChatInvestigateMixin(_OllamaChatInvestigateBase):
        async def summarize_message(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
        ) -> None:
            if await self._maybe_block_special_action_for_interaction(interaction, action_name="要約"):
                return
            await interaction.response.defer(thinking=True)

            target_text = extract_target_text_from_message(message)
            if not target_text:
                await interaction.followup.send("対象のテキストが見つかりません。")
                return
            fact_context = await build_factcheck_target_context(target_text)

            prompt = (
                "以下の文章やURL解決結果を日本語でざっくり要約してください。"
                "重要な論点だけを拾い、推測は書かないでください。\n\n"
                "【対象文章】\n"
                f"{fact_context.enriched_text or target_text}"
            )

            try:
                reply = await self._call_special_action_model(
                    prompt,
                    task_guidance="要点だけを短く自然に要約すること",
                    subject_text=fact_context.search_query_source_text or target_text,
                )
            except Exception as e:
                log.exception("summarize_message failed: %s", e)
                await interaction.followup.send("要約中にエラーが発生しました。")
                return

            description = truncate_text(reply, 4000) or "要約結果を生成できませんでした。"
            embed = discord.Embed(
                title="📝 要約結果",
                description=description,
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed)
            self._inject_context_cache(interaction, "要約", description)
            await self._store_special_action_memory(
                action_key="summarize",
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(interaction, "channel_id", None),
                subject_text=fact_context.search_query_source_text or target_text,
                result_text=description,
            )

        async def simplify_message(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
        ) -> None:
            if await self._maybe_block_special_action_for_interaction(interaction, action_name="わかりやすい解説"):
                return
            await interaction.response.defer(thinking=True)

            target_text = extract_target_text_from_message(message)
            if not target_text:
                await interaction.followup.send("対象のテキストが見つかりません。")
                return
            fact_context = await build_factcheck_target_context(target_text)

            prompt = (
                "以下の文章や発言について、それが事実かどうか（真偽判定・ファクトチェック・正誤の検証）は一切行わず、"
                "『発言者が何を言っているのか』『どういう意味・理屈なのか』を、誰にでも直感的に理解できるように日本語でわかりやすく噛み砕いて解説してください。\n\n"
                "【解説の重要ルール】\n"
                "・事実関係の正誤や信憑性を検証・判定せず、発言の言わんとしている中身自体の説明に集中すること\n"
                "・難しい専門用語や抽象的な表現は、身近な例えや平易な言葉に言い換えること\n"
                "・『要するにこういうこと』という核心を最初につかみ、仕組みや背景をわかりやすく解きほぐすこと\n\n"
                "【対象文章】\n"
                f"{fact_context.enriched_text or target_text}"
            )

            try:
                reply = await self._call_special_action_model(
                    prompt,
                    task_guidance="発言の真偽を検証せず、言っている内容や意味を身近な言葉でわかりやすく噛み砕いて解説すること",
                    subject_text=fact_context.search_query_source_text or target_text,
                )
            except Exception as e:
                log.exception("simplify_message failed: %s", e)
                await interaction.followup.send("解説中にエラーが発生しました。")
                return

            description = truncate_text(reply, 4000) or "解説結果を生成できませんでした。"
            embed = discord.Embed(
                title="📘 わかりやすい解説",
                description=description,
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed)
            self._inject_context_cache(interaction, "わかりやすい解説", description)
            await self._store_special_action_memory(
                action_key="simplify",
                guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                channel_id=getattr(interaction, "channel_id", None),
                subject_text=fact_context.search_query_source_text or target_text,
                result_text=description,
            )

        def _get_investigate_context_lines(self, cid: int | None, limit: int = 5) -> list[str]:
            if cid is None:
                return []
            cache = getattr(self, "channel_context_cache", None)
            if not isinstance(cache, dict) or cid not in cache:
                return []
            cached_q = cache[cid]
            lines: list[str] = []
            for item in list(cached_q)[-limit:]:
                line_str = str(item.get("line") or "").strip()
                if line_str:
                    lines.append(line_str)
            return lines

        def _is_self_contained_investigate_target(self, target_text: str) -> bool:
            """対象メッセージ自身が十分に自立した内容を持ち、直前雑談キャッシュを必要としないか判定する。"""
            text = str(target_text or "").strip()
            if not text:
                return False
            if re.search(r"https?://", text):
                return True
            cleaned = clean_user_input_text(text, remove_urls=True)
            substantive = re.sub(
                r"(?:これ|それ|あれ|どれ|この|その|あの|どの|本当|ほんと|マジ|嘘|うそ|デマ|確認|どう|どうなの|教えて|詳しく|調べて|ファクトチェック|って何|とは|何|なに)",
                "",
                cleaned,
            ).strip()
            if len(substantive) >= 8:
                return True
            kanji_katakana = re.findall(r"[\u4e00-\u9fff\u30a0-\u30ffA-Za-z0-9]{2,}", substantive)
            if kanji_katakana and sum(len(w) for w in kanji_katakana) >= 4:
                return True
            return False

        async def _resolve_investigate_context_lines(
            self,
            message: discord.Message,
            target_text: str,
            cid: int | None,
        ) -> list[str]:
            """調査対象メッセージの最適な文脈（返信親メッセージ最優先、自立時は直前雑談遮断）を解決する。"""
            # 1. 返信先（Reply parent）があれば最優先文脈とする
            try:
                ref_msg = await resolve_reference_message(message)
                if ref_msg:
                    ref_text = extract_target_text_from_message(ref_msg)
                    if ref_text:
                        author_name = getattr(getattr(ref_msg, "author", None), "display_name", "") or "相手"
                        return [f"{author_name}: {ref_text}"]
            except Exception as e:
                log.warning("failed to resolve reference message for investigate: %s", e)

            # 2. 対象テキスト自身が自立している場合は直前雑談キャッシュを渡さない（目的語ハイジャック防止）
            if self._is_self_contained_investigate_target(target_text):
                return []

            # 3. 自立していない短い代名詞文の場合のみ直前雑談キャッシュを参照
            return self._get_investigate_context_lines(cid, limit=5)

        async def what_is_this_message(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
        ) -> None:
            if await self._maybe_block_special_action_for_interaction(interaction, action_name="Web検索"):
                return
            owner_label = self._requester_label_from_interaction(interaction)
            acquired = await self._try_begin_factcheck(
                owner_label=owner_label,
                request_kind="これ何？(Web検索)",
            )
            if not acquired:
                busy_text = self._build_factcheck_busy_text()
                if interaction.response.is_done():
                    await interaction.followup.send(busy_text, ephemeral=True)
                else:
                    await interaction.response.send_message(busy_text, ephemeral=True)
                return

            try:
                await interaction.response.defer(thinking=True)

                target_text = extract_target_text_from_message(message)
                if not target_text:
                    await interaction.followup.send("対象のテキストが見つかりません。")
                    return
                fact_context = await build_factcheck_target_context(target_text)
                cid = getattr(interaction, "channel_id", None)
                context_lines = await self._resolve_investigate_context_lines(message, target_text, cid)
                search_query, topic_label = await self._extract_what_is_this_query(
                    target_text,
                    fact_context,
                    context_lines=context_lines,
                )

                research_result = await research_dispatch(
                    search_query,
                    context=target_text,
                    mode="auto",
                    max_results=4,
                )
                search_results = str(research_result.get("summary") or "")
                search_error = self._research_results_error_text(research_result)
                if search_error:
                    await interaction.followup.send(search_error)
                    return
                target_block = self._build_target_block(target_text, fact_context, header="【対象文章】")
                prompt = (
                    "ユーザーは次の内容について『これは何か』を知りたがっています。"
                    "検索結果をもとに、日本語で短くわかりやすく説明してください。"
                    "断定できない点はその旨も書いてください。\n\n"
                    f"{target_block}"
                    "【検索クエリ】\n"
                    f"{search_query}\n\n"
                    "【検索結果】\n"
                    f"{search_results}"
                )

                reply = await self._call_special_action_model(
                    prompt,
                    task_guidance="検索結果を踏まえて『これは何か』を短く自然に説明すること",
                    subject_text=topic_label or search_query or target_text,
                )

                description = truncate_text(reply, 4000) or "結果を生成できませんでした。"
                embed = discord.Embed(
                    title=f"🔎 『{truncate_text(topic_label, 80)}』を調べました",
                    description=description,
                    color=discord.Color.teal(),
                )
                await interaction.followup.send(embed=embed)
                self._inject_context_cache(interaction, f"{topic_label}の調査", description)
                await self._store_special_action_memory(
                    action_key="what_is_this",
                    guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                    channel_id=getattr(interaction, "channel_id", None),
                    subject_text=topic_label or search_query or target_text,
                    result_text=description,
                )
            except Exception as e:
                log.exception("what_is_this_message failed: %s", e)
                await interaction.followup.send("Web検索中にエラーが発生しました。")
            finally:
                await self._end_factcheck()

        async def _try_begin_factcheck(self, *, owner_label: str, request_kind: str) -> bool:
            async with self._factcheck_state_lock:
                if self._factcheck_in_progress:
                    return False
                self._factcheck_in_progress = True
                self._factcheck_owner_label = owner_label.strip()
                self._factcheck_owner_started_at = time.time()
                self._factcheck_request_kind = request_kind.strip()
                return True

        async def _end_factcheck(self) -> None:
            async with self._factcheck_state_lock:
                self._factcheck_in_progress = False
                self._factcheck_owner_label = ""
                self._factcheck_owner_started_at = 0.0
                self._factcheck_request_kind = ""

        def _build_factcheck_busy_text(self) -> str:
            owner = self._factcheck_owner_label or "他のユーザー"
            kind = self._factcheck_request_kind or "ファクトチェック"
            started_at = self._factcheck_owner_started_at

            elapsed_text = ""
            if started_at > 0:
                elapsed = max(int(time.time() - started_at), 0)
                if elapsed < 60:
                    elapsed_text = f"（開始から{elapsed}秒経過）"
                else:
                    minutes = elapsed // 60
                    seconds = elapsed % 60
                    elapsed_text = f"（開始から{minutes}分{seconds}秒経過）"

            return f"今は {owner} の {kind} を処理中です。結果の出力が終わるまで使えません。{elapsed_text}".strip()

        def _search_results_error_text(self, search_results: str) -> str | None:
            normalized = str(search_results or "").strip()
            if not normalized:
                return "Web検索に失敗しました。少し待ってからもう一度試してください。"
            if normalized == "検索がタイムアウトしました。":
                return "Web検索がタイムアウトしました。少し待ってからもう一度試してください。"
            if normalized.startswith("検索中にエラーが発生しました:"):
                return "Web検索中にエラーが発生しました。少し待ってからもう一度試してください。"
            return None

        def _research_results_error_text(self, research_result: dict[str, Any] | None) -> str | None:
            if not isinstance(research_result, dict):
                return self._search_results_error_text(str(research_result or ""))

            error_text = str(research_result.get("error") or "").strip()
            if error_text:
                return error_text
            return self._search_results_error_text(str(research_result.get("summary") or ""))

        def _requester_label_from_user_like(self, user_obj: object) -> str:
            if user_obj is None:
                return "unknown"

            name = getattr(user_obj, "display_name", "") or getattr(user_obj, "name", "")
            name = str(name).strip()
            if name:
                return name

            user_id = getattr(user_obj, "id", "unknown")
            return f"user:{user_id}"

        def _requester_label_from_message(self, message: discord.Message) -> str:
            author = getattr(message, "author", None)
            return self._requester_label_from_user_like(author)

        def _requester_label_from_interaction(self, interaction: discord.Interaction) -> str:
            return self._requester_label_from_user_like(getattr(interaction, "user", None))

        def _is_umigame_active_in_channel(self, channel_id_value: int | None) -> bool:
            return _is_umigame_active_channel(channel_id_value, self.umigame_states)

        async def _maybe_block_special_action_for_interaction(
            self,
            interaction: discord.Interaction,
            *,
            action_name: str,
        ) -> bool:
            if not self._is_umigame_active_in_channel(getattr(interaction, "channel_id", None)):
                return False
            text = _build_umigame_tool_block_text(action_name)
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
            return True

        async def _maybe_block_special_action_for_message(
            self,
            message: discord.Message,
            *,
            action_name: str,
        ) -> bool:
            if not self._is_umigame_active_in_channel(channel_id(message)):
                return False
            await message.reply(_build_umigame_tool_block_text(action_name), mention_author=False)
            return True

        async def fact_check_message(
            self,
            interaction: discord.Interaction,
            message: discord.Message,
        ) -> None:
            if await self._maybe_block_special_action_for_interaction(interaction, action_name="ファクトチェック"):
                return
            owner_label = self._requester_label_from_interaction(interaction)
            acquired = await self._try_begin_factcheck(
                owner_label=owner_label,
                request_kind="ファクトチェック",
            )
            if not acquired:
                busy_text = self._build_factcheck_busy_text()
                if interaction.response.is_done():
                    await interaction.followup.send(busy_text, ephemeral=True)
                else:
                    await interaction.response.send_message(busy_text, ephemeral=True)
                return

            try:
                await interaction.response.defer(thinking=True)

                target_text = extract_target_text_from_message(message)
                if not target_text:
                    await interaction.followup.send("テキストが含まれていません。")
                    return
                fact_context = await build_factcheck_target_context(target_text)

                cid = getattr(interaction, "channel_id", None)
                context_lines = await self._resolve_investigate_context_lines(message, target_text, cid)
                query = await self._extract_fact_check_search_query(
                    target_text,
                    fact_context,
                    context_lines=context_lines,
                )
                research_result = await research_dispatch(
                    query,
                    context=target_text,
                    mode="auto",
                    max_results=3,
                )
                search_results = str(research_result.get("summary") or "")
                search_error = self._research_results_error_text(research_result)
                if search_error:
                    await interaction.followup.send(search_error)
                    return
                target_block = self._build_target_block(target_text, fact_context, header="【対象の発言】")
                prompt = (
                    "以下の発言を検索結果と照らし合わせてファクトチェックする。"
                    "返答では結論を先に置き、根拠は検索結果から言える範囲だけに絞る。\n"
                    "検索結果に対象の事実が直接載っていない場合は、断定せず『検索した範囲では確認できなかった』『根拠が見当たらない』旨を落ち着いて説明する。\n"
                    "キャラクターの怯えや恐怖（『怖い』『別の話をしませんか』など）で判定を放棄・拒絶せず、真偽の判定・確認結果として回答すること。\n\n"
                    f"{target_block}"
                    "【検索結果】\n"
                    f"{search_results}"
                )
                try:
                    result = await self._call_special_action_model(
                        prompt,
                        task_guidance="真偽判定の結論を出し、その理由を短く自然に説明すること",
                        style_guard_key="OLLAMA_FACTCHECK_STYLE_GUARD",
                        subject_text=fact_context.search_query_source_text or target_text,
                    )
                except Exception as e:
                    log.exception("fact check failed: %s", e)
                    await interaction.followup.send("ファクトチェック中にエラーが発生しました。")
                    return

                description = truncate_text(result, 4000) or "判定結果を生成できませんでした。"
                embed = discord.Embed(
                    title="📝 コミュニティノート（AI判定）",
                    description=description,
                    color=discord.Color.dark_theme(),
                )
                await interaction.followup.send(embed=embed)
                self._inject_context_cache(interaction, "ファクトチェック", description)
                await self._store_special_action_memory(
                    action_key="fact_check",
                    guild_id=getattr(getattr(interaction, "guild", None), "id", None),
                    channel_id=getattr(interaction, "channel_id", None),
                    subject_text=query or fact_context.search_query_source_text or target_text,
                    result_text=description,
                )
            finally:
                await self._end_factcheck()

