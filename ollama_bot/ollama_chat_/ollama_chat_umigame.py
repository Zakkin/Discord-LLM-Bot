"""うみがめのスープゲームの開始、GM判定、ヒント、終了処理を担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..common.config_helpers import cfg
from ..common.discord_helpers import author_id, channel_id, resolve_reference_message
from lib.discord_utils import send_interaction_or_fallback
from ..common.ollama_helpers import (
    call_ollama_json,
    sanitize_generated_reply,
    truncate_text,
)
from ..common.umigame_helpers import (
    UMIGAME_JSON_SCHEMA,
    UMIGAME_GM_JSON_SCHEMA,
    UMIGAME_GM_FALLBACK_JSON_SCHEMA,
    _umigame_cfg_text,
    _umigame_memory_persona,
    _build_umigame_generation_system_prompt,
    _build_umigame_generation_user_prompt,
    _build_umigame_gm_system_prompt,
    _build_umigame_gm_fallback_system_prompt,
    _build_umigame_gm_user_prompt,
    _normalize_umigame_judgement,
    _format_umigame_gm_reply,
    _looks_like_umigame_guess,
    _infer_umigame_judgement_without_llm,
    _build_umigame_gm_generation_options,
    _strip_umigame_memory_labels,
    _build_umigame_giveup_message,
    _build_umigame_clear_append_message,
    _looks_like_umigame_question_duplicate,
    _umigame_start_message,
)
from .ollama_chat_types import MessageRuntime

if TYPE_CHECKING:
    from .ollama_chat_types import OllamaChatProtocol
    _OllamaChatUmigameBase = OllamaChatProtocol
else:
    _OllamaChatUmigameBase = object

log = logging.getLogger("ollama_bot.ollama_chat_.ollama_chat_umigame")


class OllamaChatUmigameMixin(_OllamaChatUmigameBase):
    async def _send_umigame_interaction_message(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        ephemeral: bool = False,
        defer_first: bool = False,
    ) -> bool:
        return await send_interaction_or_fallback(
            interaction,
            content,
            ephemeral=ephemeral,
            defer_first=defer_first,
            log_tag="umigame",
        )

    async def _is_umigame_reply_message(self, message: discord.Message) -> bool:
        cid = channel_id(message)
        if cid is None or cid not in self.umigame_states:
            return False
        if not getattr(message, "reference", None):
            return False

        puzzle = self.umigame_states.get(cid) or {}
        tracked_ids_raw = puzzle.get("tracked_message_ids") or set()
        tracked_ids: set[int] = set()
        for value in tracked_ids_raw:
            try:
                tracked_ids.add(int(value))
            except Exception:
                continue
        if not tracked_ids:
            return False

        current = message
        max_depth = max(int(cfg("UMIGAME_REPLY_CHAIN_MAX_DEPTH", 16) or 16), 1)
        for _ in range(max_depth):
            ref_msg = await resolve_reference_message(current)
            if not ref_msg:
                return False
            ref_id = getattr(ref_msg, "id", None)
            if ref_id is not None and int(ref_id) in tracked_ids:
                return True
            current = ref_msg
        return False

    @app_commands.command(
        name="umigame",
        description="AIが完全オリジナルで生成したウミガメのスープを開始します。"
    )
    
    async def start_umigame(self, interaction: discord.Interaction) -> None:
        cid = getattr(interaction, "channel_id", None)
        if cid is None:
            await interaction.response.send_message(
                _umigame_cfg_text("UMIGAME_CHANNEL_UNKNOWN_TEXT", "チャンネル情報を取得できませんでした。"),
                ephemeral=True,
            )
            return

        if cid in self.umigame_states:
            await interaction.response.send_message(
                _umigame_cfg_text("UMIGAME_ALREADY_RUNNING_TEXT", "現在このチャンネルではすでにゲームが進行中です。"),
                ephemeral=True,
            )
            return
        if cid in getattr(self, "twenty_doors_states", {}):
            await interaction.response.send_message(
                "現在このチャンネルでは20の扉が進行中です。先にそちらを終了してください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            pooled_item = await self.game_pool_manager.pop_umigame()
            if pooled_item:
                question = pooled_item["question"]
                answer = pooled_item["answer"]
                motif_summary = pooled_item["motif_summary"]
            else:
                log.info("umigame pool is empty, generating on demand")
                pooled_item = await self._generate_umigame_for_pool()
                if not pooled_item:
                    raise ValueError("failed to generate umigame on demand")
                question = pooled_item["question"]
                answer = pooled_item["answer"]
                motif_summary = pooled_item["motif_summary"]
        except Exception as e:
            log.error("umigame generation failed: %r", e)
            failed_text = _umigame_cfg_text("UMIGAME_GENERATION_FAILED_TEXT", "問題の生成に失敗しました。少し待ってからもう一度試してください。")
            try:
                await interaction.edit_original_response(content=failed_text)
            except Exception:
                if interaction.channel and hasattr(interaction.channel, "send"):
                    await interaction.channel.send(failed_text)
            return

        self._recent_umigame_questions.append(question)
        start_message = await interaction.edit_original_response(content=_umigame_start_message(question))
        tracked_message_ids: set[int] = set()
        start_message_id = getattr(start_message, "id", None)
        if start_message_id is not None:
            tracked_message_ids.add(int(start_message_id))
        self.umigame_states[cid] = {
            "question": question,
            "answer": answer,
            "motif_summary": motif_summary,
            "history": [],
            "tracked_message_ids": tracked_message_ids,
        }

    async def _generate_umigame_for_pool(self) -> dict[str, Any] | None:
        try:
            memory_persona = _umigame_memory_persona()
            seed_memories = await self.memory_store.get_random_memories(
                persona=memory_persona,
                guild_id=None,
                channel_id=None,
                limit=int(cfg("UMIGAME_MEMORY_SEED_COUNT", 3) or 3),
                min_score=float(cfg("UMIGAME_MEMORY_SEED_MIN_SCORE", 0.0) or 0.0),
                exclude_memory_types=["feedback", "training_candidate"],
            )
            recent_questions = list(self._recent_umigame_questions)
            model_name = str(cfg("UMIGAME_GENERATION_MODEL", cfg("OLLAMA_MAIN_MODEL", cfg("OLLAMA_MODEL", ""))) or "").strip()

            result_json: dict[str, Any] | None = None
            question = ""
            answer = ""
            motif_summary = ""
            attempts = max(int(cfg("UMIGAME_GENERATION_ATTEMPTS", 3) or 3), 1)
            base_num_predict = max(int(cfg("UMIGAME_GENERATION_NUM_PREDICT", 768) or 768), 256)
            json_retries = max(int(cfg("UMIGAME_GENERATION_RETRIES", cfg("OLLAMA_JSON_RETRIES", 1)) or 1), 0)
            generation_prompt = _build_umigame_generation_user_prompt(
                seed_memories=seed_memories,
                recent_questions=recent_questions,
            )
            last_reject_reason = "unknown"
            for attempt_idx in range(attempts):
                attempt_num_predict = min(base_num_predict + (attempt_idx * 256), 2048)
                try:
                    result_json = await call_ollama_json(
                        generation_prompt,
                        system_prompt=_build_umigame_generation_system_prompt(),
                        schema=UMIGAME_JSON_SCHEMA,
                        model=model_name,
                        think=False,
                        timeout_sec=float(cfg("UMIGAME_GENERATION_TIMEOUT_SEC", 90.0) or 90.0),
                        retries=json_retries,
                        temperature=float(cfg("UMIGAME_GENERATION_TEMPERATURE", 0.95) or 0.95),
                        top_p=float(cfg("UMIGAME_GENERATION_TOP_P", 0.95) or 0.95),
                        repeat_penalty=float(cfg("UMIGAME_GENERATION_REPEAT_PENALTY", 1.03) or 1.03),
                        num_predict=attempt_num_predict,
                    )
                except RuntimeError as e:
                    message = str(e)
                    if "done_reason=length" in message or "truncated=True" in message:
                        last_reject_reason = "truncated"
                        log.warning(
                            "umigame generation truncated: attempt=%s/%s model=%s num_predict=%s",
                            attempt_idx + 1,
                            attempts,
                            model_name,
                            attempt_num_predict,
                        )
                        continue
                    raise

                question = _strip_umigame_memory_labels(str(result_json.get("question", "") or "").strip())
                answer = _strip_umigame_memory_labels(str(result_json.get("answer", "") or "").strip())
                motif_summary = _strip_umigame_memory_labels(str(result_json.get("motif_summary", "") or "").strip())
                logical_trick = _strip_umigame_memory_labels(str(result_json.get("logical_trick", "") or "").strip())
                source_memory_indices_raw = result_json.get("source_memory_indices") or []
                source_memory_indices: list[int] = []
                for value in source_memory_indices_raw:
                    try:
                        idx = int(value)
                    except Exception:
                        continue
                    if 1 <= idx <= len(seed_memories) and idx not in source_memory_indices:
                        source_memory_indices.append(idx)
                if not question or not answer or not logical_trick:
                    last_reject_reason = "empty_field"
                    log.warning("umigame generation rejected: attempt=%s/%s reason=empty_field", attempt_idx + 1, attempts)
                    continue
                if seed_memories and not source_memory_indices:
                    last_reject_reason = "ungrounded_memory"
                    log.warning(
                        "umigame generation rejected: attempt=%s/%s reason=ungrounded_memory",
                        attempt_idx + 1,
                        attempts,
                    )
                    continue
                if len(question) < 20 or len(answer) < 20:
                    last_reject_reason = "too_short"
                    log.warning(
                        "umigame generation rejected: attempt=%s/%s reason=too_short qlen=%s alen=%s",
                        attempt_idx + 1,
                        attempts,
                        len(question),
                        len(answer),
                    )
                    continue
                if _looks_like_umigame_question_duplicate(question, recent_questions):
                    last_reject_reason = "duplicate_recent"
                    log.warning(
                        "umigame generation rejected: attempt=%s/%s reason=duplicate_recent question=%r",
                        attempt_idx + 1,
                        attempts,
                        truncate_text(question, 120),
                    )
                    continue
                break
            else:
                log.warning(f"failed to generate unique umigame puzzle: last_reason={last_reject_reason}")
                return None

            question = truncate_text(_strip_umigame_memory_labels(question), 400)
            answer = truncate_text(_strip_umigame_memory_labels(answer), 1200)
            motif_summary = truncate_text(motif_summary, 180)
            
            return {
                "question": question,
                "answer": answer,
                "motif_summary": motif_summary,
                "logical_trick": logical_trick
            }
        except Exception as e:
            log.error("umigame pool generation failed: %r", e)
            return None



    @app_commands.command(
        name="umigame_giveup",
        description="進行中のウミガメのスープをギブアップして真相を表示します。"
    )
    async def giveup_umigame(self, interaction: discord.Interaction) -> None:
        cid = getattr(interaction, "channel_id", None)
        if cid is None or cid not in self.umigame_states:
            await self._send_umigame_interaction_message(
                interaction,
                _umigame_cfg_text("UMIGAME_NOT_RUNNING_TEXT", "現在進行中のゲームはありません。"),
                ephemeral=True,
            )
            return

        puzzle = self.umigame_states.get(cid)
        if not puzzle:
            await self._send_umigame_interaction_message(
                interaction,
                _umigame_cfg_text("UMIGAME_NOT_RUNNING_TEXT", "現在進行中のゲームはありません。"),
                ephemeral=True,
            )
            return

        reply_text = _build_umigame_giveup_message(str(puzzle.get("answer", "") or ""))
        sent = await self._send_umigame_interaction_message(
            interaction,
            reply_text,
            defer_first=True,
        )
        if sent:
            self.umigame_states.pop(cid, None)

    async def _answer_umigame(self, *, channel_id_value: int, user_text: str) -> tuple[str, bool]:
        puzzle = self.umigame_states[channel_id_value]
        history = puzzle.setdefault("history", [])
        prompt = _build_umigame_gm_user_prompt(user_text)
        question = puzzle["question"]
        answer = puzzle["answer"]
        gm_model_name = str(
            cfg(
                "UMIGAME_GM_MODEL",
                cfg("OLLAMA_MAIN_MODEL", cfg("OLLAMA_MODEL", "")),
            ) or ""
        ).strip()
        primary_options = _build_umigame_gm_generation_options()
        fallback_options = _build_umigame_gm_generation_options(fallback=True)
        heuristic_judgement = _infer_umigame_judgement_without_llm(
            user_text,
            answer,
            history,
            question=question,
        )

        if heuristic_judgement is not None:
            reply = _format_umigame_gm_reply(heuristic_judgement)
            history.append({"q": truncate_text(user_text, 160), "a": truncate_text(reply, 120)})
            if len(history) > int(cfg("UMIGAME_HISTORY_MAX_ITEMS", 20) or 20):
                del history[:-int(cfg("UMIGAME_HISTORY_MAX_ITEMS", 20) or 20)]
            return reply, heuristic_judgement == "clear"

        try:
            result = await call_ollama_json(
                prompt,
                system_prompt=_build_umigame_gm_system_prompt(question, answer),
                schema=UMIGAME_GM_JSON_SCHEMA,
                model=gm_model_name,
                think=False,
                timeout_sec=float(cfg("UMIGAME_GM_TIMEOUT_SEC", 45.0) or 45.0),
                retries=int(cfg("UMIGAME_GM_RETRIES", 1) or 1),
                temperature=float(primary_options["temperature"]),
                top_p=float(primary_options["top_p"]),
                repeat_penalty=float(primary_options["repeat_penalty"]),
                num_predict=int(primary_options["num_predict"]),
            )
        except Exception as e:
            log.warning("umigame gm primary failed; fallback mode starts: %r", e)
            try:
                result = await call_ollama_json(
                    prompt,
                    system_prompt=_build_umigame_gm_fallback_system_prompt(question, answer),
                    schema=UMIGAME_GM_FALLBACK_JSON_SCHEMA,
                    model=gm_model_name,
                    think=False,
                    timeout_sec=float(cfg("UMIGAME_GM_FALLBACK_TIMEOUT_SEC", 20.0) or 20.0),
                    retries=int(cfg("UMIGAME_GM_FALLBACK_RETRIES", 1) or 1),
                    temperature=float(fallback_options["temperature"]),
                    top_p=float(fallback_options["top_p"]),
                    repeat_penalty=float(fallback_options["repeat_penalty"]),
                    num_predict=int(fallback_options["num_predict"]),
                )
            except Exception as fallback_error:
                log.warning("umigame gm fallback failed too; heuristic mode starts: %r", fallback_error)
                result = {"judgement": _infer_umigame_judgement_without_llm(user_text, answer, history, question=question) or "irrelevant"}

        judgement = _normalize_umigame_judgement(result.get("judgement", "irrelevant"))
        if judgement == "irrelevant" and _looks_like_umigame_guess(user_text):
            inferred = _infer_umigame_judgement_without_llm(
                user_text,
                answer,
                history,
                question=question,
            )
            if inferred is not None:
                judgement = inferred
        reply = _format_umigame_gm_reply(judgement)
        history.append({"q": truncate_text(user_text, 160), "a": truncate_text(reply, 120)})
        if len(history) > int(cfg("UMIGAME_HISTORY_MAX_ITEMS", 20) or 20):
            del history[:-int(cfg("UMIGAME_HISTORY_MAX_ITEMS", 20) or 20)]
        is_clear = judgement == "clear"
        return reply, is_clear


    async def _handle_active_umigame_message(self, message: discord.Message, runtime: MessageRuntime) -> bool:
        cid = runtime.cid
        if cid is None or cid not in self.umigame_states:
            return False
        if not bool(getattr(runtime, "is_umigame_reply", False)):
            return False

        user_text = (runtime.effective_user_text or runtime.original_user_text or "").strip()
        if not user_text:
            return True

        try:
            reply, is_clear = await self._answer_umigame(channel_id_value=cid, user_text=user_text)
        except Exception as e:
            log.warning("umigame gm failed: %r", e)
            judgement = _infer_umigame_judgement_without_llm(
                user_text,
                self.umigame_states.get(cid, {}).get("answer", ""),
                self.umigame_states.get(cid, {}).get("history", []),
                question=self.umigame_states.get(cid, {}).get("question", ""),
            )
            reply = _format_umigame_gm_reply(judgement or "unknown")
            is_clear = False

        if is_clear:
            puzzle = self.umigame_states.pop(cid, None)
            if puzzle:
                reply = f"{reply}{_build_umigame_clear_append_message(str(puzzle.get('answer', '') or ''))}"

        self._append_current_user_message(runtime, message)

        safe_reply_text = sanitize_generated_reply(reply)
        try:
            sent = await message.reply(safe_reply_text, mention_author=True)
        except Exception as e:
            log.warning("umigame forced reply failed, falling back to normal send: %r", e)
            sent = await message.channel.send(safe_reply_text)

        tracked_message_ids = self.umigame_states.get(cid, {}).setdefault("tracked_message_ids", set())
        if isinstance(tracked_message_ids, set):
            message_id = getattr(message, "id", None)
            sent_id = getattr(sent, "id", None)
            if message_id is not None:
                tracked_message_ids.add(int(message_id))
            if sent_id is not None:
                tracked_message_ids.add(int(sent_id))

        self._append_sent_message(sent)
        return True
