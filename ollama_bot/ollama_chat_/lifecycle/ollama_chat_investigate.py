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
from lib.text_utils import normalize_compare_text
from lib.config_utils import cfg_persona_namespace

from ..ollama_chat_helpers import *
from ..ollama_chat_helpers import (
    _build_umigame_tool_block_text,
    _is_umigame_active_channel,
)
from ...common.ollama_helpers import (
    _strip_think_blocks,
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
)
from harness.agent.prompt_parser.parser.reply_helpers import _strip_user_echo_prefix
from harness.agent.tool_registry.adapters.special_action import (
    fallback_extract_keywords as _fallback_extract_keywords,
    is_special_action_parrot as _is_special_action_parrot,
    is_special_action_query_hijacked as _is_special_action_query_hijacked,
    sanitize_special_action_query_hallucinations as _sanitize_special_action_query_hallucinations,
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
        self._schedule_investigate_emotion_update(message)

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
        self._schedule_investigate_emotion_update(message)

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
            self._schedule_investigate_emotion_update(message)
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

    def _schedule_investigate_emotion_update(self, message: discord.Message) -> None:
        """特殊アクション完了時に感情更新・Bio同期を安全に呼び出す。"""
        schedule_fn = getattr(self, "_schedule_emotion_update", None)
        if callable(schedule_fn):
            try:
                schedule_fn(message)
                return
            except Exception as e:
                log.warning("failed to schedule emotion update for investigate action: %s", e)
                return

        run_fn = getattr(self, "_run_emotion_update_for_message", None)
        if callable(run_fn):
            try:
                asyncio.create_task(run_fn(message))
                return
            except Exception as e:
                log.warning("failed to run emotion update for investigate action: %s", e)
                return

        update_state_fn = getattr(self, "_update_emotion_state", None)
        sync_presence_fn = getattr(self, "_sync_emotion_presence", None)
        if callable(update_state_fn) and callable(sync_presence_fn):
            async def _run_direct_update() -> None:
                try:
                    get_runtime_fn = getattr(self, "_get_or_build_runtime", None)
                    runtime = await get_runtime_fn(message) if callable(get_runtime_fn) else None
                    if runtime is not None:
                        await update_state_fn(runtime)
                        await sync_presence_fn()
                except Exception as e:
                    log.warning("failed to directly update emotion state for investigate action: %s", e)
            asyncio.create_task(_run_direct_update())

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
                "【絶対厳守: 比喩・ネットミーム・スラングの字義通り解釈の禁止】\n"
                "・対象文章に含まれる比喩、ネットミーム、スラング、皮肉、逆説的表現（例: 『リアル嘘つきノーランド』＝作中で嘘つきと呼ばれながら実は真実を語っていた人物になぞらえた称賛表現など）を、文字通りの意味（単なる嘘つき等）として字義通りに受け取って真偽判定しないこと。\n"
                "・比喩やミームが含まれる場合は、その比喩が意味している実質的な主張（例: 『引退後に考察が実際に的中したか』）を検証すること。\n"
                "【絶対厳守: 検索結果外の解説・捏造の禁止】\n"
                "・検索結果に直接記載されていない事実（サービスや組織の運営元・親会社・子会社、名称の由来・語源、関係者の情報など）について、自身の記憶や推測で勝手に補足・解説・断定することは固く禁じます。\n"
                "・検索結果に根拠がない場合は、『検索した範囲では、〜という情報は確認できませんでした』『〜に関する根拠は見当たりませんでした』とだけ落ち着いて説明し、それ以上の背景知識を語らないでください。\n"
                "【絶対厳守: 検索結果タイトルや正式名称による用語置換・ハイジャックの禁止】\n"
                "・検索結果スニペットのタイトルや見出し（例: pixiv百科事典の正式名称等）に引きずられて、ユーザーが使った言葉（俗称・あだ名・比喩表現など）を勝手に別の正式名称やすり替えられた名詞（例: 『リアル嘘つきノーランド』を『モンブラン・ノーランド』等）に置き換えて言及することを固く禁じます。必ず対象の発言で主張されている通りの表現・言葉を用いて真偽を説明すること。\n"
                "・検索結果で見つかったWebサイト名（例: 『pixiv百科事典の〜』等）を不自然に主語に据えたり、検索結果の個別記事名に対象をすり替えて論評しないこと。\n"
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
            self._schedule_investigate_emotion_update(message)
        finally:
            await self._end_factcheck()

    async def _handle_fact_check_interaction(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        """ファクトチェックのinteractionハンドラ（fact_check_messageへの委譲）。"""
        await self.fact_check_message(interaction, message)

    def _special_action_num_predict(self) -> int:
        base = cfg_int("OLLAMA_REPLY_NUM_PREDICT", 128)
        reply_num_predict = getattr(self, "_reply_num_predict", None)
        if callable(reply_num_predict):
            with contextlib.suppress(Exception):
                resolved = reply_num_predict()
                if resolved is not None:
                    base = int(resolved)
        if base <= 0:
            base = 128
        configured = int(cfg("OLLAMA_SPECIAL_ACTION_NUM_PREDICT", 2048) or 2048)
        return max(base, configured)

    def _special_action_system_prompt(
        self,
        *,
        task_guidance: str,
        style_guard_key: str = "OLLAMA_INVESTIGATE_STYLE_GUARD",
    ) -> str:
        prompt_blocks: list[str] = []

        base_persona = str(cfg("OLLAMA_SYSTEM_PROMPT", "") or "").strip()
        if base_persona:
            prompt_blocks.append(f"【キャラクター設定】\n{base_persona}")

        style_guard = self._resolve_special_action_style_guard(style_guard_key)
        if style_guard:
            prompt_blocks.append(f"【口調ガード】\n{style_guard}")

        extra_rule = str(cfg("OLLAMA_REPLY_EXTRA_RULE", "") or "").strip()
        if extra_rule:
            prompt_blocks.append(f"【追加ルール】\n{extra_rule}")

        extra = (
            "【特殊機能の重要ルール】\n"
            "・調査・要約・解説を行う際も、システムプロンプトで設定されたキャラクターとして自然に話してください。あなたはシステムプロンプトで設定されたキャラクター本人として振る舞ってください。\n"
            "・AIとしての冷たい客観解説口調（「〜であります」「〜と推測されます」等）は避け、システムプロンプトで設定されたキャラクター本人の自然な口調・一人称で話してください（キャラクター設定上の文末語尾はそのまま維持してください）。\n"
            "・相手の最新メッセージの内容や指示をそのまま自分のセリフの冒頭で繰り返す（エコー・オウム返し）ことは禁止です。\n"
            "・Discordで読みやすいよう短すぎず、結論や反応を先に置きつつ要点・理由を2〜4文程度で自然に説明してください。\n"
            "・箇条書き、見出し、注釈、Markdown記法は使わず、セリフ本文だけを返してください。\n"
            f"今回の役割: {task_guidance}"
        )
        prompt_blocks.append(extra)
        return "\n\n".join(block for block in prompt_blocks if block).strip()

    def _append_special_action_rules(self, prompt: str) -> str:
        rules = (
            f"{str(prompt or '').rstrip()}\n\n"
            "【出力ルール】\n"
            "・外部の検索結果やURL解決結果がある場合は、そこに記載された事実や見出し・概要から言える範囲を基にし、推測や嘘を混ぜないでください。\n"
            "・検索結果に直接記載されていない背景知識や企業名・由来などを勝手に推測・捏造して語ることは厳禁です。\n"
            "・対象の発言で使われている固有名詞や表現（例: 通称・俗称・比喩表現）を、検索結果で見つかった別の正式名称（例: pixiv百科事典等の見出し名）やすり替えられた名詞に勝手に置き換えて結論を述べないこと。必ず対象の発言で主張されている通りの表現・言葉を用いて真偽を説明してください。\n"
            "・対象の発言に含まれる比喩やネットミーム（例: 『リアル嘘つきノーランド』などの逆説的称賛）を文字通りに解釈して真逆の判定を行わないでください。\n"
            "・思考プロセスや思考タグ（think）、前置きや解説は出力せず、キャラクター本人の日本語の返答本文だけを直接出力してください。\n"
            "・箇条書き、見出し、注釈、Markdown記法は使わず、セリフ本文だけを出力してください。"
        )
        return append_chat_reply_output_suffix(rules)

    def _resolve_special_action_style_guard(self, style_guard_key: str) -> str:
        style_guard = str(cfg(style_guard_key, "") or "").strip()
        if not style_guard and style_guard_key != "OLLAMA_INVESTIGATE_STYLE_GUARD":
            style_guard = str(cfg("OLLAMA_INVESTIGATE_STYLE_GUARD", "") or "").strip()
        if not style_guard:
            style_guard = str(cfg("OLLAMA_REPLY_STYLE_GUARD", "") or "").strip()
        return style_guard

    def _special_action_style_profile(self, style_guard: str) -> str:
        return str(cfg("SPECIAL_ACTION_STYLE_PROFILE", "") or "").strip().lower()

    def _special_action_reply_needs_style_rewrite(
        self,
        reply: str,
        *,
        style_guard: str,
        style_guard_key: str,
    ) -> bool:
        text = str(reply or "").strip()
        if not text or style_guard_key != "OLLAMA_FACTCHECK_STYLE_GUARD":
            return False

        formal_tail = re.search(
            r"(?:結論としては|判断できます|判断できません|存在しません|不可能です|なります|"
            r"(?<!なん)(?<!なの)(?<!ん)です[。．!！?？]|ます[。．!！?？]|でしょう[。．!！?？])",
            text,
        )
        if not formal_tail:
            return False

        allowed_markers = cfg("STYLE_REWRITE_ALLOWED_MARKERS", ())
        if allowed_markers:
            has_allowed_marker = any(marker in text for marker in allowed_markers)
            return not has_allowed_marker

        profile = self._special_action_style_profile(style_guard)
        if profile or ("丁寧" in style_guard and "禁止" in style_guard) or "崩した" in style_guard or "砕けた" in style_guard:
            return True
        return bool("丁寧" in style_guard and "禁止" in style_guard)

    def _build_special_action_style_rewrite_prompt(
        self,
        reply: str,
        *,
        style_guard: str,
        task_guidance: str,
    ) -> str:
        return (
            "以下のファクトチェック結果は、事実関係と結論は使えるが、キャラクターの口調が崩れています。\n"
            "検索結果の事実・判定・根拠の強弱は一切変えず、口調だけを口調ガードに合わせて書き直してください。\n"
            "新しい根拠や推測を足さないでください。箇条書き、見出し、Markdown、前置きは禁止です。\n\n"
            f"【今回の役割】\n{task_guidance}\n\n"
            f"【口調ガード】\n{style_guard}\n\n"
            "【書き直す文章】\n"
            f"{reply}"
        )

    def _should_reroute_short_target_to_lookup(self, target_text: str) -> bool:
        normalized = " ".join(str(target_text or "").split())
        if not normalized:
            return False

        max_chars = max(int(cfg("OLLAMA_SPECIAL_ACTION_SHORT_TARGET_MAX_CHARS", 80) or 80), 1)
        if len(normalized) > max_chars:
            return False
        if re.search(r"https?://", normalized, flags=re.IGNORECASE):
            return True
        if len(normalized) <= 32:
            return True
        if "\n" not in normalized and not re.search(r"[。.!?！？]", normalized):
            return True
        return False

    def _resolve_special_action_mode_for_target(
        self,
        mode: str,
        target_text: str,
        *,
        user_instruction: str = "",
    ) -> str:
        resolved_mode = str(mode or "").strip()
        if user_instruction and resolved_mode in {"summarize", "simplify"}:
            norm_inst = re.sub(r"\s+", "", user_instruction).lower()
            if any(kw in norm_inst for kw in (
                "って何", "ってなに", "て何", "てなに", "とは何", "とはなに",
                "ってなんだ", "てなんだ", "って誰", "て誰", "ってどこ", "てどこ",
                "ってどういうこと", "ってどういう意味", "とは",
            )):
                log.info(
                    "special action mode rerouted from instruction: mode=%s -> what_is_this instruction=%r",
                    resolved_mode,
                    truncate_text(user_instruction, 80),
                )
                return "what_is_this"

        if resolved_mode not in {"summarize"}:
            return resolved_mode
        if not self._should_reroute_short_target_to_lookup(target_text):
            return resolved_mode

        log.info(
            "special action mode rerouted: mode=%s -> what_is_this target_len=%s target=%r",
            resolved_mode,
            len(" ".join(str(target_text or "").split())),
            truncate_text(target_text, 80),
        )
        return "what_is_this"

    def _build_special_action_recovery_prompt(
        self,
        prompt: str,
        *,
        task_guidance: str,
    ) -> str:
        return (
            f"{str(prompt or '').rstrip()}\n\n"
            "【重要・直接返答指示】\n"
            "思考プロセスや思考タグ（think）、前置き、挨拶、注釈は一切出力しないでください。\n"
            "対象の文章をそのまま繰り返す（オウム返し）ことは禁止です。\n"
            "今すぐ、キャラクター本人の言葉として判定の結論と理由を短く（2〜3文で）直接出力してください。"
        )

    async def _call_special_action_model(
        self,
        prompt: str,
        *,
        task_guidance: str,
        style_guard_key: str = "OLLAMA_INVESTIGATE_STYLE_GUARD",
        message: discord.Message | None = None,
        subject_text: str = "",
        think: bool | None = None,
    ) -> str:
        guarded_prompt = self._append_special_action_rules(prompt)
        system_prompt = self._special_action_system_prompt(
            task_guidance=task_guidance,
            style_guard_key=style_guard_key,
        )
        num_predict = self._special_action_num_predict()

        call_with_typing = message is not None
        if call_with_typing:
            raw_reply = await self._call_with_typing(
                message,
                guarded_prompt,
                think=think,
                system_prompt=system_prompt,
                num_predict=num_predict,
            )
        else:
            raw_reply = await self._call_model(
                guarded_prompt,
                think=think,
                system_prompt=system_prompt,
                num_predict=num_predict,
            )

        cleaned = self._sanitize_special_action_reply(raw_reply, original_prompt=prompt, subject_text=subject_text)

        needs_recovery = (
            not cleaned
            or ("<think" in str(raw_reply or "").lower() and not _strip_think_blocks(str(raw_reply or "")).strip())
        )
        if needs_recovery:
            recovery_prompt = self._build_special_action_recovery_prompt(prompt, task_guidance=task_guidance)
            try:
                log.info("special action reply empty, think-only, or parroting; attempting direct-output recovery retry")
                if call_with_typing:
                    recovery_raw = await self._call_with_typing(
                        message,
                        recovery_prompt,
                        think=think,
                        system_prompt=system_prompt,
                        num_predict=num_predict,
                    )
                else:
                    recovery_raw = await self._call_model(
                        recovery_prompt,
                        think=think,
                        system_prompt=system_prompt,
                        num_predict=num_predict,
                    )
                recovery_cleaned = self._sanitize_special_action_reply(
                    recovery_raw,
                    original_prompt=recovery_prompt,
                    subject_text=subject_text,
                )
                if recovery_cleaned:
                    cleaned = recovery_cleaned
            except Exception as e:
                log.warning("special action recovery retry failed: %r", e)

        if not cleaned:
            fallback = str(
                cfg("OLLAMA_REASONING_LEAK_FALLBACK", "今のはうまく言葉になっていません。")
                or "今のはうまく言葉になっていません。"
            ).strip()
            cleaned = extract_first_user_facing_reply(fallback) or sanitize_generated_reply(fallback) or "今のはうまく言葉になっていません。"

        style_guard = self._resolve_special_action_style_guard(style_guard_key)
        if self._special_action_reply_needs_style_rewrite(
            cleaned,
            style_guard=style_guard,
            style_guard_key=style_guard_key,
        ):
            rewrite_prompt = self._build_special_action_style_rewrite_prompt(
                cleaned,
                style_guard=style_guard,
                task_guidance=task_guidance,
            )
            try:
                log.info("special action reply needed style rewrite; executing second pass")
                rewritten_raw = await self._call_model(
                    rewrite_prompt,
                    think=False,
                    system_prompt=system_prompt,
                    num_predict=num_predict,
                )
                rewritten_cleaned = self._sanitize_special_action_reply(
                    rewritten_raw,
                    original_prompt=rewrite_prompt,
                    subject_text=subject_text,
                )
                if rewritten_cleaned and not self._special_action_reply_needs_style_rewrite(
                    rewritten_cleaned,
                    style_guard=style_guard,
                    style_guard_key=style_guard_key,
                ):
                    cleaned = rewritten_cleaned
            except Exception as e:
                log.warning("special action style rewrite failed: %r", e)

        return cleaned

    @staticmethod
    def _restore_factcheck_subject_terms(reply: str, subject_text: str) -> str:
        """ファクトチェック出力において、対象文章の比喩・俗称表現が検索結果の正式名称にすり替えられている場合に復元する。"""
        if not reply or not subject_text:
            return reply

        # 1. 「リアル嘘つきノーランド / うそつきノーランド」 -> 「モンブラン・ノーランド」すり替え検知と復元
        norland_match = re.search(r"(?:リアル)?(?:嘘つき|うそつき)ノーランド", subject_text)
        if norland_match and "モンブラン" not in subject_text:
            original_norland = norland_match.group(0)
            # 「pixiv百科事典のモンブラン・ノーランド」等の見出し由来表現もケア
            reply = re.sub(r"(?:pixiv|ピクシブ)百科事典の\s*モンブラン・ノーランド", original_norland, reply)
            reply = reply.replace("モンブラン・ノーランド", original_norland)

        return reply

    def _sanitize_special_action_reply(self, reply: str, original_prompt: str = "", subject_text: str = "") -> str:
        cleaned = extract_first_user_facing_reply(str(reply or ""))
        if subject_text:
            cleaned = _strip_user_echo_prefix(subject_text, cleaned)
            cleaned = self._restore_factcheck_subject_terms(cleaned, subject_text)
        elif original_prompt:
            cleaned = _strip_user_echo_prefix(original_prompt, cleaned)

        if cleaned and not looks_like_abnormal_assistant_reply(cleaned):
            if subject_text and _is_special_action_parrot(subject_text, cleaned):
                log.warning("special action sanitized reply was a pure parrot of subject_text: %r", truncate_text(cleaned, 80))
                return ""
            return cleaned
        return ""

    def _build_special_action_memory_payload(
        self,
        *,
        action_key: str,
        subject_text: str,
        result_text: str,
    ) -> tuple[str, str, float] | None:
        clean_subject = " ".join(str(subject_text or "").split())
        clean_result = " ".join(str(result_text or "").split())
        if not clean_result:
            return None
        if "生成できませんでした" in clean_result:
            return None

        compact_subject = truncate_text(clean_subject, 100) or "この話題"
        compact_result = truncate_text(clean_result, 320)

        is_unconfirmed_factcheck = False
        if str(action_key or "").strip() == "fact_check":
            unconfirmed_markers = (
                "根拠が確認でき", "根拠は見当たり", "確認できません", "情報は見当たり",
                "確認できなかった", "根拠は見当たらな", "見当たらない", "確認できない",
                "根拠は確認でき",
            )
            if any(m in clean_result for m in unconfirmed_markers):
                is_unconfirmed_factcheck = True

        action_templates: dict[str, tuple[str, str, float]] = {
            "fact_check": (
                "conversation_theme" if is_unconfirmed_factcheck else "fact",
                "以前ファクトチェックした話題「{subject}」の結論: {result}",
                0.50 if is_unconfirmed_factcheck else 0.88,
            ),
            "what_is_this": (
                "fact",
                "以前Web調査した話題「{subject}」の要点: {result}",
                0.82,
            ),
            "summarize": (
                "conversation_theme",
                "以前要約した話題「{subject}」の要点: {result}",
                0.66,
            ),
            "simplify": (
                "conversation_theme",
                "以前わかりやすく整理した話題「{subject}」の要点: {result}",
                0.68,
            ),
        }
        payload = action_templates.get(str(action_key or "").strip())
        if not payload:
            return None

        memory_type, template, score = payload
        content = template.format(subject=compact_subject, result=compact_result).strip()
        if not content:
            return None
        return memory_type, content, score

    async def _store_special_action_memory(
        self,
        *,
        action_key: str,
        guild_id: int | None,
        channel_id: int | None,
        subject_text: str,
        result_text: str,
    ) -> None:
        if not cfg("MEMORY_ENABLED", True):
            return
        if not hasattr(self, "memory_store") or not self.memory_store:
            return

        payload = self._build_special_action_memory_payload(
            action_key=action_key,
            subject_text=subject_text,
            result_text=result_text,
        )
        if not payload:
            return

        memory_type, content, score = payload
        persona = cfg_persona_namespace(custom_cfg=cfg)
        try:
            await self.memory_store.add_memory(
                persona=persona,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=None,
                memory_type=memory_type,
                content=content,
                score=score,
            )
            log.info(
                "special action memory stored: action=%s memory_type=%s guild=%s channel=%s subject=%r",
                action_key,
                memory_type,
                guild_id,
                channel_id,
                truncate_text(subject_text, 80),
            )
            add_episode = getattr(self.memory_store, "add_episode", None)
            if callable(add_episode) and cfg_bool("MEMORY_EPISODE_ENABLED", True):
                await add_episode(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    user_id=None,
                    summary=f"{action_key} を実行した。",
                    what=truncate_text(subject_text, 600),
                    bot_action=truncate_text(result_text, 600),
                    user_reaction=None,
                    importance=max(float(score or 0.0), float(cfg("MEMORY_EPISODE_BASE_IMPORTANCE", 0.42) or 0.42)),
                    tags=["episode", "special_action", str(action_key or "").strip()],
                )
        except Exception as e:
            log.warning("special action memory store failed: action=%s err=%r", action_key, e)

    def _fallback_extract_keywords(
        self,
        target_text: str,
        *,
        context_lines: list[str] | None = None,
        user_instruction: str = "",
    ) -> str:
        def _clean(t: str) -> str:
            cleaned = clean_user_input_text(t, remove_urls=True)
            cleaned = re.sub(r"[!！?？^○^~〜()（）「」『』【】\[\]<>]+", " ", cleaned)
            stop_phrases = (
                "教えてあげる", "教えてほしい", "教えて", "教えろ", "について",
                "んですよね", "なんですよね", "つまりそう言う事", "そういうこと",
                "どういうこと", "どういう意味", "本当ですか", "本当か", "嘘ですか",
                "デマですか", "検証して", "調べて", "ファクトチェック", "って何なのか",
                "って何", "ってなに", "とは何", "とはなに", "とは",
            )
            for sp in stop_phrases:
                cleaned = cleaned.replace(sp, " ")
            return re.sub(r"\s+", " ", cleaned).strip()

        inst_clean = _clean(user_instruction) if user_instruction else ""
        target_clean = _clean(target_text)

        skip_words = {"いいえ", "はい", "そう", "うん", "ううん", "ない", "ある", "これ", "それ", "あれ", "どれ"}
        context_terms: list[str] = []
        if context_lines:
            for line in reversed(context_lines[-4:]):
                cleaned_line = _clean(line)
                if ":" in cleaned_line:
                    cleaned_line = cleaned_line.split(":", 1)[-1].strip()
                words = [w for w in re.split(r"[\s、。,.]+", cleaned_line) if len(w) >= 2 and w not in skip_words]
                for w in words:
                    if w not in target_clean and w not in inst_clean and w not in context_terms:
                        context_terms.append(w)
                    if len(context_terms) >= 2:
                        break
                if len(context_terms) >= 2:
                    break

        parts: list[str] = []
        if inst_clean:
            parts.append(inst_clean)
        if target_clean and target_clean != inst_clean:
            parts.append(target_clean)
        if context_terms:
            parts.extend(reversed(context_terms))

        combined = " ".join(parts).strip()
        return truncate_text(combined, 60) or truncate_text(user_instruction or target_text, 30)

    async def _extract_fact_check_search_query(
        self,
        target_text: str,
        fact_context,
        *,
        context_lines: list[str] | None = None,
        user_instruction: str = "",
    ) -> str:
        keyword_schema = {
            "type": "object",
            "properties": {
                "search_query": {
                    "type": "string",
                    "description": "ウェブ検索用のスペース区切りの短いキーワード",
                }
            },
            "required": ["search_query"],
            "additionalProperties": False,
        }
        context_blocks: list[str] = []
        if context_lines:
            recent_context = "\n".join(context_lines[-5:])
            context_blocks.append(f"【直前の会話文脈】\n{recent_context}")
        if user_instruction:
            context_blocks.append(f"【ユーザーの指示・着眼点】\n{user_instruction}")

        extra_context = "\n\n".join(context_blocks)
        if extra_context:
            extra_context = f"{extra_context}\n\n"

        keyword_prompt = (
            "以下の文章から、真偽を確かめるためのウェブ検索キーワードを抽出してください。\n"
            "【最重要ルール】\n"
            "・検索キーワードは必ず【対象の文章】の内容・固有名詞・主要キーワード（事件・サービス名・主題など）を主軸にしてください。\n"
            "・【対象の文章】に書かれていない無関係な単語・固有名詞・勝手な連想（関連しそうな別ゲーム・別サービス・思いつきなど）を混入させることは絶対に禁止です。必ず対象文章に実際に存在する語句や、主張の核となる語句（サービス名・語源・主題など）のみを抽出してください。\n"
            "・比喩やネットミーム表現（例: 『リアル嘘つきノーランド』など）が含まれる場合、文字通りの意味にとらわれず、検証すべき実質的な主張・主題（対象人物・事件・的中・引退など）のキーワードを抽出してください。\n"
            "・直前の会話文脈にある無関係な単語（他人の雑談の主語や固有名詞など）を混入させて対象文章の話題をすり替えること（目的語ハイジャック）は厳禁です。\n"
            "・直前の会話文脈は、【対象の文章】が『これ』『それ』などの代名詞だけで対象が完全に不明な場合にのみ補完として参照してください。対象の文章自体に名詞や具体的な内容がある場合は、直前の会話文脈を無視してください。\n"
            "・長文を避け、重要な単語を2〜4つだけスペース区切りで出力してください。\n\n"
            f"{extra_context}"
            f"対象の文章: {fact_context.enriched_text or target_text}"
        )

        try:
            extraction_result = await call_ollama_json(
                keyword_prompt,
                system_prompt=(
                    "あなたは優秀な検索アシスタントです。"
                    "事実確認に最適な検索キーワードのみをJSONで返します。"
                ),
                schema=keyword_schema,
                model=str(cfg("OLLAMA_CLASSIFIER_MODEL", "classifier-local") or "classifier-local"),
                temperature=float(cfg("OLLAMA_FACT_CHECK_KEYWORD_TEMPERATURE", 0.0) or 0.0),
                think=False,
                timeout_sec=float(cfg("OLLAMA_FACT_CHECK_TIMEOUT_SEC", 45) or 45),
                retries=int(cfg("OLLAMA_FACT_CHECK_RETRIES", 1) or 1),
            )
            search_query = str(extraction_result.get("search_query") or "").strip()
        except Exception as e:
            log.error("fact-check keyword extraction failed: %r", e)
            search_query = ""

        source_text = fact_context.search_query_source_text or target_text
        if search_query:
            sanitized_query = _sanitize_special_action_query_hallucinations(
                search_query,
                source_text,
                context_lines=context_lines,
                user_instruction=user_instruction,
            )
            if sanitized_query != search_query:
                log.info(
                    "fact-check search query sanitized: raw=%r -> sanitized=%r",
                    search_query,
                    sanitized_query,
                )
                search_query = sanitized_query

        if search_query and _is_special_action_query_hijacked(
            search_query,
            source_text,
            user_instruction=user_instruction,
        ):
            log.warning(
                "fact-check search query hijacked by context! extracted=%r target=%r; falling back to target keywords",
                search_query,
                truncate_text(target_text, 80),
            )
            search_query = ""

        if not search_query:
            search_query = self._fallback_extract_keywords(
                source_text,
                context_lines=context_lines if not _is_special_action_query_hijacked(
                    "dummy", source_text, user_instruction=user_instruction
                ) else None,
                user_instruction=user_instruction,
            )
        return search_query

    async def _extract_what_is_this_query(
        self,
        target_text: str,
        fact_context,
        *,
        context_lines: list[str] | None = None,
        user_instruction: str = "",
    ) -> tuple[str, str]:
        keyword_schema = {
            "type": "object",
            "properties": {
                "search_query": {
                    "type": "string",
                    "description": "検索に使う短い語句",
                },
                "topic_label": {
                    "type": "string",
                    "description": "回答見出しに使う短い名称",
                },
            },
            "required": ["search_query", "topic_label"],
            "additionalProperties": False,
        }
        context_blocks = []
        if context_lines:
            recent_context = "\n".join(context_lines[-5:])
            context_blocks.append(f"【直前の会話文脈】\n{recent_context}")
        if user_instruction:
            context_blocks.append(f"【ユーザーの指示・質問】\n{user_instruction}")

        extra_context = "\n\n".join(context_blocks)
        if extra_context:
            extra_context = f"{extra_context}\n\n"

        try:
            extraction_result = await call_ollama_json(
                (
                    "以下の文章やユーザーの質問について『これは何か』を調べたいです。\n"
                    "検索ノイズを減らせる短い検索語と、表示用の短い名称をJSONで返してください。\n"
                    "【最重要ルール】\n"
                    "・検索語は必ず【対象文章】の内容・固有名詞を最優先にしてください。無関係な直前文脈の単語で話題をすり替えること（目的語ハイジャック）は禁止です。\n"
                    "・【対象文章】に書かれていない無関係な単語や勝手な連想を混入させることは絶対に禁止です。\n"
                    "・ユーザーの質問に対象語（例: 『TRPGって何？』の『TRPG』）が含まれている場合は、その対象語を最優先で検索語にしてください。\n"
                    "・直前の会話文脈は、対象文章が代名詞等で対象不明な場合にのみ補完として参照してください。\n"
                    "・検索語は1個または2個までの重要語に絞ってください。\n\n"
                    f"{extra_context}"
                    f"対象文章: {fact_context.enriched_text or target_text}"
                ),
                system_prompt=(
                    "あなたは検索語抽出アシスタントです。"
                    "説明文は返さず、必ずJSONのみを返してください。"
                ),
                schema=keyword_schema,
                model=str(cfg("OLLAMA_CLASSIFIER_MODEL", "classifier-local") or "classifier-local"),
                temperature=float(cfg("OLLAMA_FACT_CHECK_KEYWORD_TEMPERATURE", 0.0) or 0.0),
                think=False,
                timeout_sec=float(cfg("OLLAMA_FACT_CHECK_TIMEOUT_SEC", 45) or 45),
                retries=int(cfg("OLLAMA_FACT_CHECK_RETRIES", 1) or 1),
            )
            search_query = str(extraction_result.get("search_query") or "").strip()
            topic_label = str(extraction_result.get("topic_label") or "").strip()
        except Exception as e:
            log.warning("what_is_this keyword extraction failed: %s", e)
            search_query = ""
            topic_label = ""

        source_text = fact_context.search_query_source_text or target_text
        if search_query:
            sanitized_query = _sanitize_special_action_query_hallucinations(
                search_query,
                source_text,
                context_lines=context_lines,
                user_instruction=user_instruction,
            )
            if sanitized_query != search_query:
                log.info(
                    "what_is_this search query sanitized: raw=%r -> sanitized=%r",
                    search_query,
                    sanitized_query,
                )
                search_query = sanitized_query

        if search_query and _is_special_action_query_hijacked(
            search_query,
            source_text,
            user_instruction=user_instruction,
        ):
            log.warning(
                "what_is_this search query hijacked by context! extracted=%r target=%r; falling back to target keywords",
                search_query,
                truncate_text(target_text, 80),
            )
            search_query = ""
            topic_label = ""

        if not search_query:
            search_query = self._fallback_extract_keywords(
                source_text,
                context_lines=context_lines if not _is_special_action_query_hijacked(
                    "dummy", source_text, user_instruction=user_instruction
                ) else None,
                user_instruction=user_instruction,
            )
        if not topic_label:
            topic_label = search_query
        return search_query, topic_label

    def _is_reply_investigate_request(self, message: discord.Message) -> bool:
        if not getattr(self, "bot", None) or not self.bot.user:
            return False
        bot_user_id = getattr(self.bot.user, "id", None)
        author_is_bot = bool(getattr(message.author, "bot", False))
        if author_is_bot:
            if bot_user_id is not None and author_id(message) == bot_user_id:
                return False
            if not has_anon_buttons(message):
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
        return classify_reply_investigate_mode(text) is not None

    def _extract_reply_investigate_instruction(
        self,
        message: discord.Message,
        *,
        fallback: str = "この内容を簡潔に調べてください。",
    ) -> str:
        text = message.clean_content or content(message) or ""
        if getattr(self, "bot", None) and self.bot.user:
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

        try:
            await message.add_reaction("🔍")
            target_message = ref_msg or await resolve_reference_message(message)
            if not target_message:
                await message.reply("返信先のメッセージが見つかりません。", mention_author=False)
                return

            target_text = extract_target_text_from_message(target_message)
            if not target_text:
                await message.reply("返信先からテキストを抽出できませんでした。", mention_author=False)
                return

            fact_context = await build_factcheck_target_context(target_text)
            target_block = self._build_target_block(target_message, fact_context)

            if resolved_mode == "summarize":
                prompt = (
                    "以下の文章を日本語でざっくり要約してください。\n\n"
                    "【ユーザーの依頼】\n"
                    f"{user_instruction}\n\n"
                    f"{target_block}"
                )
                result_text = await self._call_special_action_model(
                    prompt,
                    task_guidance="文章の要点を短く箇条書きまたは自然な日本語でまとめること",
                    message=message,
                )
                description = truncate_text(result_text, 4000) or "要約結果を生成できませんでした。"
                embed = discord.Embed(
                    title="📋 要約結果",
                    description=description,
                    color=discord.Color.blue(),
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
                self._schedule_investigate_emotion_update(message)
                return

            if resolved_mode == "simplify":
                prompt = (
                    "以下の内容を、小学生でもわかるようにわかりやすく解説してください。\n\n"
                    "【指示】\n"
                    f"{user_instruction}\n\n"
                    "【対象文章】\n"
                    f"{fact_context.enriched_text or target_text}"
                )
                result_text = await self._call_special_action_model(
                    prompt,
                    task_guidance="難しい内容を砕いて、短くわかりやすく説明すること",
                    message=message,
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
                self._schedule_investigate_emotion_update(message)
                return

            if resolved_mode == "what_is_this":
                search_query, topic_label = await self._extract_what_is_this_query(target_text, fact_context)
                log.info("generated what-is-this search query: %s", search_query)
                research_result = await research_dispatch(
                    search_query,
                    context=target_text,
                    mode="auto",
                    max_results=4,
                )
            else:
                search_query = await self._extract_fact_check_search_query(target_text, fact_context)
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
                self._schedule_investigate_emotion_update(message)
                return

            prompt = (
                "以下の発言を検索結果と照らし合わせてファクトチェックする。"
                "ユーザーの依頼も踏まえつつ、返答では結論を先に置き、根拠は検索結果から言える範囲だけに絞る。\n"
                "検索結果に対象の事実が直接載っていない場合は、断定せず『検索した範囲では確認できなかった』『根拠が見当たらない』旨を落ち着いて説明する。\n"
                "【絶対厳守: 比喩・ネットミーム・スラングの字義通り解釈の禁止】\n"
                "・対象文章に含まれる比喩、ネットミーム、スラング、皮肉、逆説的表現（例: 『リアル嘘つきノーランド』＝作中で嘘つきと呼ばれながら実は真実を語っていた人物になぞらえた称賛表現など）を、文字通りの意味（単なる嘘つき等）として字義通りに受け取って真偽判定しないこと。\n"
                "・比喩やミームが含まれる場合は、その比喩が意味している実質的な主張（例: 『引退後に考察が実際に的中したか』）を検証すること。\n"
                "【絶対厳守: 検索結果タイトルや正式名称による用語置換・ハイジャックの禁止】\n"
                "・検索結果スニペットのタイトルや見出し（例: pixiv百科事典の正式名称等）に引きずられて、ユーザーが使った言葉（俗称・あだ名・比喩表現など）を勝手に別の正式名称やすり替えられた名詞（例: 『リアル嘘つきノーランド』を『モンブラン・ノーランド』等）に置き換えて言及することを固く禁じます。必ず対象の発言で主張されている通りの表現・言葉を用いて真偽を説明すること。\n"
                "・検索結果で見つかったWebサイト名（例: 『pixiv百科事典の〜』等）を不自然に主語に据えたり、検索結果の個別記事名に対象をすり替えて論評しないこと。\n\n"
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
                    subject_text=fact_context.search_query_source_text or target_text,
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
            self._schedule_investigate_emotion_update(message)
        except Exception as e:
            log.exception("reply investigate failed: %s", e)
            await message.reply("特別処理の実行中にエラーが発生しました。", mention_author=False)
        finally:
            await self._safe_remove_own_reaction(message, "🔍")
            await self._end_factcheck()


OllamaChatReplyInvestigateMixin = OllamaChatInvestigateMixin



