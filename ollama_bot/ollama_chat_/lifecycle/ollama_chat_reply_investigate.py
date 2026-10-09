"""リプライ（返信）経由での調査指示（例：「これ要約して」）の解釈と実行フローを担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import time
import re
from typing import Optional, Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..ollama_chat_helpers import *

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _OllamaChatReplyInvestigateBase = OllamaChatProtocol
else:
    _OllamaChatReplyInvestigateBase = object


def is_standalone_reply_question(text: str) -> bool:
    """リプライ（返信）時のテキストが、直前メッセージに対する解説・要約要求（メタ操作）ではなく、

    新しいトピックについての質問文（主語や目的語を含んだ自立した質問・通常対話）であるかを判定する。
    """
    raw = str(text or "").strip()
    if not raw:
        return False

    # 1. 操作手順・How-to・利用方法・ダウンロード等の自立したアクション/ノウハウ要求
    # 例: 「XでGifをダウンロードする方法を教えて」「〜のやり方は？」「〜の使い方」
    howto_markers = (
        "方法", "やり方", "やりかた", "仕方", "しかた", "手順", "使い方", "つかいかた",
        "ダウンロード", "download", "保存", "ログイン", "サインイン", "login", "signin",
        "インストール", "設定方法", "どうすれば", "どうやって", "するには",
    )
    if any(marker in raw for marker in howto_markers):
        return True

    # 2. メタ操作キーワード（指示代名詞、解説/要約/検証の指示語、副詞など）を除去し、
    # 残ったテキストに自立した名詞・目的語（新規トピック）が含まれているかを検査する。
    # 「これをもっとわかりやすく解説して」 -> メタ語のみ（コアテキストが空または極小） -> False (メタ操作)
    # 「XでGifをダウンロードする方法を教えて」 -> "XGifダウンロード方法" 等が残る -> True (自立質問)
    # 「量子コンピュータについて詳しく教えて」 -> "量子コンピュータ" が残る -> True (自立質問)
    meta_patterns = [
        r"これ(ら)?", r"それ(ら)?", r"あれ(ら)?", r"上記(の(発言|内容|文|文章))?",
        r"この(メッセージ|発言|内容|文章|文|やつ|件)?", r"さっきの(メッセージ|発言|内容|文章|文)?",
        r"直前の(メッセージ|発言|内容|文章|文)?", r"前言",
        r"もっと", r"さらに", r"もうちょっと", r"簡単に", r"かんたんに",
        r"わかりやすく", r"分かりやすく", r"やさしく", r"優しく", r"初心者向け(に)?",
        r"詳しく", r"kwsk", r"3行で", r"三行で", r"3文で", r"三文で",
        r"要約(して)?", r"まとめ(て)?", r"解説(して)?", r"説明(して)?", r"教えて",
        r"検証(して)?", r"裏取り(して)?", r"ファクトチェック(して)?",
        r"本当(か)?", r"ほんと(か)?", r"嘘(でしょ)?", r"デマ(か)?",
    ]
    core_text = raw
    for pat in meta_patterns:
        core_text = re.sub(pat, " ", core_text, flags=re.IGNORECASE)

    # 助詞や記号・空白を除去
    core_text = re.sub(r"[\s\u3000、。,.!！?？をでにへとのはが|/\\〜~]+", "", core_text)

    # コアテキストに自立した語句（2文字以上の名詞・キーワード）が残っている場合は新規トピック・自立質問とみなす
    return len(core_text) >= 2


class OllamaChatReplyInvestigateMixin(_OllamaChatReplyInvestigateBase):
        def _is_reply_investigate_request(self, message: discord.Message) -> bool:
            if not self.bot.user:
                return False
            bot_user_id = getattr(self.bot.user, "id", None)
            if bot_user_id is not None and author_id(message) == bot_user_id:
                return False
            author_is_bot = bool(getattr(message.author, "bot", False))
            if author_is_bot and not has_anon_buttons(message):
                return False
            if not getattr(message, "reference", None):
                return False
            try:
                mentions = getattr(message, "mentions", None) or []
            except Exception:
                mentions = []
            if bot_user_id is None or not any(getattr(user, "id", None) == bot_user_id for user in mentions):
                return False

            text = self._extract_reply_investigate_instruction(message, fallback="")
            if not text:
                return False

            # 新しいトピックについての質問文（主語や目的語を含んだ自立した質問）である場合は、
            # 直前メッセージの解説機能（simplify等）を起動せず、通常の会話応答サイクルへルーティングする。
            if is_standalone_reply_question(text):
                return False

            return classify_reply_investigate_mode(text) is not None

        def _extract_reply_investigate_instruction(
            self,
            message: discord.Message,
            *,
            fallback: str = "この内容を簡潔に調べてください。",
        ) -> str:
            text = message.clean_content or content(message) or ""
            if self.bot.user:
                mention_patterns = [
                    re.escape(self.bot.user.mention),
                    re.escape(f"<@!{self.bot.user.id}>"),
                    re.escape(f"<@{self.bot.user.id}>"),
                    re.escape(f"@{self.bot.user.display_name}"),
                    re.escape(f"@{self.bot.user.name}"),
                ]
                for pattern in mention_patterns:
                    text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
            text = re.sub(r"\s+", " ", text).strip()
            return text or fallback

        async def _handle_reply_investigate(
            self,
            message: discord.Message,
            *,
            ref_msg: discord.Message | None = None,
            request_mode: str | None = None,
        ) -> None:
            user_instruction = self._extract_reply_investigate_instruction(message)
            resolved_mode = request_mode or classify_reply_investigate_mode(user_instruction) or "what_is_this"
            action_name = {
                "summarize": "返信要約",
                "simplify": "返信わかりやすい解説",
                "what_is_this": "返信これ何？(Web検索)",
                "fact_check": "返信ファクトチェック",
            }[resolved_mode]
            if await self._maybe_block_special_action_for_message(message, action_name=action_name):
                return
            owner_label = self._requester_label_from_message(message)
            acquired = await self._try_begin_factcheck(
                owner_label=owner_label,
                request_kind=action_name,
            )
            if not acquired:
                await message.reply(self._build_factcheck_busy_text(), mention_author=False)
                return

            await self._safe_add_reaction(message, "🔍")

            try:
                target_message = ref_msg or await resolve_reference_message(message)
                if not target_message:
                    await message.reply("返信元のメッセージを取得できませんでした。")
                    return

                target_text = extract_target_text_from_message(target_message)
                if not target_text:
                    await message.reply(
                        "返信元のメッセージにテキストが見つかりませんでした。",
                        mention_author=False,
                    )
                    return
                resolved_mode = self._resolve_special_action_mode_for_target(
                    resolved_mode,
                    target_text,
                    user_instruction=user_instruction,
                )
                fact_context = await build_factcheck_target_context(target_text)
                target_block = self._build_target_block(target_text, fact_context, header="【対象の発言】")

                if resolved_mode == "summarize":
                    prompt = (
                        "以下の文章やURL解決結果を、ユーザーの指示に沿って日本語で要約してください。"
                        "重要な論点だけを整理し、推測は書かないでください。\n\n"
                        "【指示】\n"
                        f"{user_instruction}\n\n"
                        "【対象文章】\n"
                        f"{fact_context.enriched_text or target_text}"
                    )
                    result_text = await self._call_special_action_model(
                        prompt,
                        task_guidance="要点だけを短く自然に要約すること",
                        message=message,
                        subject_text=fact_context.search_query_source_text or target_text,
                    )
                    description = truncate_text(result_text, 4000) or "要約結果を生成できませんでした。"
                    embed = discord.Embed(
                        title="📝 要約結果",
                        description=description,
                        color=discord.Color.blurple(),
                    )
                    await self._reply_with_embed_or_fallback(
                        message,
                        embed=embed,
                        fallback_heading="【要約結果】",
                        fallback_text=result_text,
                    )
                    self._inject_message_context_cache(message, "要約", description)
                    await self._store_special_action_memory(
                        action_key="summarize",
                        guild_id=getattr(getattr(message, "guild", None), "id", None),
                        channel_id=getattr(getattr(message, "channel", None), "id", None),
                        subject_text=fact_context.search_query_source_text or target_text,
                        result_text=description,
                    )
                    return

                if resolved_mode == "simplify":
                    prompt = (
                        "以下の文章や発言について、ユーザーの指示に沿って、それが事実かどうか（真偽判定・ファクトチェック・正誤の検証）は一切行わず、"
                        "『発言者が何を言っているのか』『どういう意味・理屈なのか』を、誰にでも直感的に理解できるように日本語でわかりやすく噛み砕いて解説してください。\n\n"
                        "【解説の重要ルール】\n"
                        "・事実関係の正誤や信憑性を検証・判定せず、発言の言わんとしている中身自体の説明に集中すること\n"
                        "・難しい専門用語や抽象的な表現は、身近な例えや平易な言葉に言い換えること\n"
                        "・『要するにこういうこと』という核心を最初につかみ、仕組みや背景をわかりやすく解きほぐすこと\n"
                        "・対象文章をそのまま丸写し・オウム返しすることは厳禁です\n\n"
                        "【指示】\n"
                        f"{user_instruction}\n\n"
                        "【対象文章】\n"
                        f"{fact_context.enriched_text or target_text}"
                    )
                    result_text = await self._call_special_action_model(
                        prompt,
                        task_guidance="発言の真偽を検証せず、言っている内容や意味を身近な言葉でわかりやすく噛み砕いて解説すること",
                        message=message,
                        subject_text=fact_context.search_query_source_text or target_text,
                    )
                    description = truncate_text(result_text, 4000) or "解説結果を生成できませんでした。"
                    embed = discord.Embed(
                        title="📘 わかりやすい解説",
                        description=description,
                        color=discord.Color.green(),
                    )
                    await self._reply_with_embed_or_fallback(
                        message,
                        embed=embed,
                        fallback_heading="【わかりやすい解説】",
                        fallback_text=result_text,
                    )
                    self._inject_message_context_cache(message, "わかりやすい解説", description)
                    await self._store_special_action_memory(
                        action_key="simplify",
                        guild_id=getattr(getattr(message, "guild", None), "id", None),
                        channel_id=getattr(getattr(message, "channel", None), "id", None),
                        subject_text=fact_context.search_query_source_text or target_text,
                        result_text=description,
                    )
                    return

                cid = getattr(getattr(message, "channel", None), "id", None)
                context_lines: list[str] = []
                if cid is not None and hasattr(self, "channel_context_cache") and cid in self.channel_context_cache:
                    cached_q = self.channel_context_cache[cid]
                    for item in list(cached_q)[-5:]:
                        line_str = str(item.get("line") or "").strip()
                        if line_str:
                            context_lines.append(line_str)

                if resolved_mode == "what_is_this":
                    search_query, topic_label = await self._extract_what_is_this_query(
                        target_text,
                        fact_context,
                        context_lines=context_lines,
                        user_instruction=user_instruction,
                    )
                    log.info("generated what-is-this search query: %s", search_query)
                    research_result = await research_dispatch(
                        search_query,
                        context=target_text,
                        mode="auto",
                        max_results=4,
                    )
                else:
                    search_query = await self._extract_fact_check_search_query(
                        target_text,
                        fact_context,
                        context_lines=context_lines,
                        user_instruction=user_instruction,
                    )
                    topic_label = ""
                    log.info("generated fact-check search query: %s", search_query)
                    research_result = await research_dispatch(
                        search_query,
                        context=target_text,
                        mode="auto",
                        max_results=3,
                    )

                search_results = str(research_result.get("summary") or "")
                search_error = self._research_results_error_text(research_result)
                if search_error:
                    await message.reply(search_error, mention_author=False)
                    return

                if resolved_mode == "what_is_this":
                    prompt = (
                        "ユーザーは次の内容について『これは何か』を知りたがっています。"
                        "ユーザーの依頼と検索結果をもとに、日本語で短くわかりやすく説明してください。"
                        "断定できない点はその旨も書いてください。\n\n"
                        "【ユーザーの依頼】\n"
                        f"{user_instruction}\n\n"
                        f"{target_block}"
                        "【検索クエリ】\n"
                        f"{search_query}\n\n"
                        "【検索結果】\n"
                        f"{search_results}"
                    )
                    result_text = await self._call_special_action_model(
                        prompt,
                        task_guidance="検索結果を踏まえて『これは何か』を短く自然に説明すること",
                        message=message,
                        subject_text=topic_label or search_query or target_text,
                    )
                    description = truncate_text(result_text, 4000) or "結果を生成できませんでした。"
                    embed = discord.Embed(
                        title=f"🔎 『{truncate_text(topic_label, 80)}』を調べました",
                        description=description,
                        color=discord.Color.teal(),
                    )
                    await self._reply_with_embed_or_fallback(
                        message,
                        embed=embed,
                        fallback_heading=f"【『{topic_label}』を調べました】",
                        fallback_text=result_text,
                    )
                    self._inject_message_context_cache(message, f"{topic_label}の調査", description)
                    await self._store_special_action_memory(
                        action_key="what_is_this",
                        guild_id=getattr(getattr(message, "guild", None), "id", None),
                        channel_id=getattr(getattr(message, "channel", None), "id", None),
                        subject_text=topic_label or search_query or target_text,
                        result_text=description,
                    )
                    return

                prompt = (
                    "以下の発言を検索結果と照らし合わせてファクトチェックする。"
                    "ユーザーの依頼も踏まえつつ、返答では結論を先に置き、根拠は検索結果から言える範囲だけに絞る。\n"
                    "検索結果に対象の事実が直接載っていない場合は、断定せず『検索した範囲では確認できなかった』『根拠が見当たらない』旨を落ち着いて説明する。\n"
                    "【絶対厳守: 検索結果外の解説・捏造の禁止】\n"
                    "・検索結果に直接記載されていない事実（サービスや組織の運営元・親会社・子会社、名称の由来・語源、関係者の情報など）について、自身の記憶や推測で勝手に補足・解説・断定することは固く禁じます。\n"
                    "・検索結果に根拠がない場合は、『検索した範囲では、〜という情報は確認できませんでした』『〜に関する根拠は見当たりませんでした』とだけ落ち着いて説明し、それ以上の背景知識を語らないでください。\n"
                    "キャラクターの怯えや恐怖（『怖い』『別の話をしませんか』など）で判定を放棄・拒絶せず、真偽の判定・確認結果として回答すること。\n\n"
                    "【ユーザーの依頼】\n"
                    f"{user_instruction}\n\n"
                    f"{target_block}"
                    "【検索結果】\n"
                    f"{search_results}"
                )
                try:
                    result_text = await self._call_special_action_model(
                        prompt,
                        task_guidance="検索結果に基づいて真偽を見極め、結論を先に短く伝えること",
                        style_guard_key="OLLAMA_FACTCHECK_STYLE_GUARD",
                        message=message,
                    )
                except Exception as e:
                    log.exception("reply fact check failed: %s", e)
                    await message.reply("ファクトチェック中にエラーが発生しました。", mention_author=False)
                    return

                description = truncate_text(result_text, 4000) or "判定結果を生成できませんでした。"
                embed = discord.Embed(
                    title="📝 コミュニティノート（AI判定）",
                    description=description,
                    color=discord.Color.dark_theme(),
                )
                await self._reply_with_embed_or_fallback(
                    message,
                    embed=embed,
                    fallback_heading="【ファクトチェック結果】",
                    fallback_text=result_text,
                )
                self._inject_message_context_cache(message, "ファクトチェック", description)
                await self._store_special_action_memory(
                    action_key="fact_check",
                    guild_id=getattr(getattr(message, "guild", None), "id", None),
                    channel_id=getattr(getattr(message, "channel", None), "id", None),
                    subject_text=search_query or fact_context.search_query_source_text or target_text,
                    result_text=description,
                )
            except Exception as e:
                log.exception("reply investigate failed: %s", e)
                await message.reply("特別処理の実行中にエラーが発生しました。", mention_author=False)
            finally:
                await self._safe_remove_own_reaction(message, "🔍")
                await self._end_factcheck()

