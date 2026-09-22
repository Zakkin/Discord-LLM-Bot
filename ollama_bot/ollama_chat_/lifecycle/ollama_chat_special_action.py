"""特殊アクション（要約、これ何等）のプロンプト構築、LLM呼び出し、メモリ保存などの中核ロジックを担当するMixin。"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import time
import re
from typing import Optional, Any, TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from lib.discord_utils import clean_user_input_text
from lib.text_utils import normalize_compare_text

from ..ollama_chat_helpers import *
from ...common.ollama_helpers import _strip_think_blocks
from ...common.reply_helpers import _strip_user_echo_prefix


def _is_special_action_parrot(target_text: str, reply_text: str) -> bool:
    t = normalize_compare_text(target_text)
    r = normalize_compare_text(reply_text)
    if not t or not r:
        return False
    if t == r:
        return True
    if len(t) >= 8:
        ratio = difflib.SequenceMatcher(None, t, r).ratio()
        if ratio >= 0.75:
            return True
        if t in r and len(t) >= int(len(r) * 0.70):
            return True
        if r in t and len(r) >= int(len(t) * 0.70):
            return True
    return False

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _OllamaChatSpecialActionBase = OllamaChatProtocol
else:
    _OllamaChatSpecialActionBase = object


class OllamaChatSpecialActionMixin(_OllamaChatSpecialActionBase):
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
            return (
                f"{str(prompt or '').rstrip()}\n\n"
                "【出力ルール】\n"
                "・外部の検索結果やURL解決結果がある場合は、そこに記載された事実や見出し・概要から言える範囲を基にし、推測や嘘を混ぜないでください。\n"
                "・思考プロセスや思考タグ（think）、前置きや解説は出力せず、キャラクター本人の日本語の返答本文だけを直接出力してください。\n"
                "・箇条書き、見出し、注釈、Markdown記法は使わず、セリフ本文だけを出力してください。"
            )

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

            # 設定された許可マーカー（語尾・口調の除外マーカー等）があるか確認
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
            # ユーザー指示文に用語・概念質問パターン（「って何」「とは」「について教えて」等）がある場合、what_is_this に昇格
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

            # 思考のみ出力やトークン切れ、対象文章のオウム返し・丸写しで本文が取れずフォールバックになりそうな場合、直接出力特化プロンプトで1回リカバリー生成を試みる
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
                    if call_with_typing:
                        rewritten_raw = await self._call_with_typing(
                            message,
                            rewrite_prompt,
                            think=think,
                            system_prompt=system_prompt,
                            num_predict=num_predict,
                        )
                    else:
                        rewritten_raw = await self._call_model(
                            rewrite_prompt,
                            think=think,
                            system_prompt=system_prompt,
                            num_predict=num_predict,
                        )
                    rewritten = self._sanitize_special_action_reply(
                        rewritten_raw,
                        original_prompt=rewrite_prompt,
                        subject_text=subject_text,
                    )
                    if rewritten and not self._special_action_reply_needs_style_rewrite(
                        rewritten,
                        style_guard=style_guard,
                        style_guard_key=style_guard_key,
                    ):
                        return rewritten
                    log.warning("special action style rewrite still looked formal or invalid; using original cleaned reply")
                except Exception as e:
                    log.warning("special action style rewrite failed: %r", e)
            return cleaned

        def _sanitize_special_action_reply(self, reply: str, original_prompt: str = "", subject_text: str = "") -> str:
            cleaned = extract_first_user_facing_reply(str(reply or ""))
            if subject_text:
                cleaned = _strip_user_echo_prefix(subject_text, cleaned)
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
            action_templates: dict[str, tuple[str, str, float]] = {
                "fact_check": (
                    "fact",
                    "以前ファクトチェックした話題「{subject}」の結論: {result}",
                    0.88,
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
            persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip() or "default"
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

            parts = list(reversed(context_terms))
            if inst_clean:
                parts.append(inst_clean)
            if target_clean and target_clean != inst_clean:
                parts.append(target_clean)
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
                "以下の文章から、真偽を確かめるためのウェブ検索キーワードを抽出してください。"
                "長文を避け、重要な単語を2〜4つだけスペース区切りで出力してください。\n"
                "ユーザーの指示や直前の会話文脈がある場合は、作品名や話題の対象（主語・固有名詞）を補って検索キーワードを作成してください。\n\n"
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
                    think=False,
                    timeout_sec=float(cfg("OLLAMA_FACT_CHECK_TIMEOUT_SEC", 45) or 45),
                    retries=int(cfg("OLLAMA_FACT_CHECK_RETRIES", 1) or 1),
                )
                search_query = str(extraction_result.get("search_query") or "").strip()
            except Exception as e:
                log.error("fact-check keyword extraction failed: %r", e)
                search_query = ""

            if not search_query:
                search_query = self._fallback_extract_keywords(
                    fact_context.search_query_source_text or target_text,
                    context_lines=context_lines,
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
                        "以下の文章やユーザーの質問について『これは何か』を調べたいです。"
                        "検索ノイズを減らせる短い検索語と、表示用の短い名称をJSONで返してください。"
                        "ユーザーの質問に対象語（例: 『TRPGって何？』の『TRPG』）が含まれている場合は、その対象語を最優先で検索語にしてください。\n"
                        "検索語は1個または2個までの重要語に絞ってください。\n"
                        "直前の会話文脈がある場合は、話題の対象（主語・固有名詞）を補ってください。\n\n"
                        f"{extra_context}"
                        f"対象文章: {fact_context.enriched_text or target_text}"
                    ),
                    system_prompt=(
                        "あなたは検索語抽出アシスタントです。"
                        "説明文は返さず、必ずJSONのみを返してください。"
                    ),
                    schema=keyword_schema,
                    model=str(cfg("OLLAMA_CLASSIFIER_MODEL", "classifier-local") or "classifier-local"),
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

            if not search_query:
                search_query = self._fallback_extract_keywords(
                    fact_context.search_query_source_text or target_text,
                    context_lines=context_lines,
                    user_instruction=user_instruction,
                )
            if not topic_label:
                topic_label = search_query
            return search_query, topic_label

