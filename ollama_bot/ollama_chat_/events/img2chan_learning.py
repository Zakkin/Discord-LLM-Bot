"""img.2chan.netのそうだね反応を学習メモへ変換するMixin。
LLM呼び出しの直列化と、高評価レスから安全に記憶へ保存する処理を担当します。
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

import discord

from ...common.discord_helpers import channel_id
from ...common.ollama_helpers import truncate_text
from .img2chan_context import (
    _get_img2chan_helper,
    call_ollama_json,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    log,
)

IMG2CHAN_SOUDANE_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "should_store": {"type": "boolean"},
        "memory_type": {"type": "string"},
        "pattern_summary": {"type": "string"},
        "future_hint": {"type": "string"},
        "avoid_hint": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": [
        "should_store",
        "memory_type",
        "pattern_summary",
        "future_hint",
        "avoid_hint",
        "confidence",
    ],
    "additionalProperties": False,
}

_IMG2CHAN_LEARNING_SOURCE_BLOCK_PATTERNS = re.compile(
    r"(?:ガイジ|障害者|乳首|パンティ|ちんこ|まんこ|精液|死ね|殺す|知能が虫以下|ゴミ荒らし|くっさ|ざっこ|ゲボ)",
    flags=re.IGNORECASE,
)
_IMG2CHAN_LEARNING_GENERIC_OUTPUT_PATTERNS = re.compile(
    r"(?:安心する|共感|理解を示す|距離を縮め|円滑に進め|受け入れ|寄り添|信頼を築)",
    flags=re.IGNORECASE,
)
_IMG2CHAN_LEARNING_PLACEHOLDER_PATTERNS = re.compile(r"^(?:\.{3,}|…{2,}|なし|特になし|不明)$")



from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _Img2chanLearningBase = OllamaChatProtocol
else:
    _Img2chanLearningBase = object


class _Img2chanLearningMixin(_Img2chanLearningBase):
    def _img2chan_soudane_learning_enabled(self) -> bool:
        return cfg_bool("IMG2CHAN_SOUDANE_LEARNING_ENABLED", True)

    def _img2chan_soudane_learning_min_soudane(self) -> int:
        return max(cfg_int("IMG2CHAN_SOUDANE_ANALYSIS_MIN_SOUDANE", 1), 1)

    def _img2chan_soudane_learning_max_posts_per_poll(self) -> int:
        return max(cfg_int("IMG2CHAN_SOUDANE_ANALYSIS_MAX_POSTS_PER_POLL", 2), 1)

    def _img2chan_soudane_learning_reanalyze_delta(self) -> int:
        return max(cfg_int("IMG2CHAN_SOUDANE_ANALYSIS_REANALYZE_DELTA", 2), 1)

    def _img2chan_soudane_analysis_timeout_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_SOUDANE_ANALYSIS_TIMEOUT_SEC", 45.0), 10.0)

    def _img2chan_soudane_analysis_retries(self) -> int:
        return max(cfg_int("IMG2CHAN_SOUDANE_ANALYSIS_RETRIES", 1), 0)

    def _img2chan_soudane_analysis_min_confidence(self) -> float:
        return max(0.0, min(cfg_float("IMG2CHAN_SOUDANE_ANALYSIS_MIN_CONFIDENCE", 0.45), 1.0))

    def _img2chan_llm_limit(self) -> int:
        return max(cfg_int("IMG2CHAN_LLM_MAX_CONCURRENT", 1), 1)

    def _get_img2chan_llm_semaphore(self) -> asyncio.Semaphore:
        limit = self._img2chan_llm_limit()
        semaphore = getattr(self, "_img2chan_llm_semaphore", None)
        current_limit = getattr(self, "_img2chan_llm_semaphore_limit", None)
        if not isinstance(semaphore, asyncio.Semaphore) or current_limit != limit:
            semaphore = asyncio.Semaphore(limit)
            self._img2chan_llm_semaphore = semaphore
            self._img2chan_llm_semaphore_limit = limit
        return semaphore

    def _img2chan_llm_queue_snapshot(self) -> tuple[int, int]:
        semaphore = self._get_img2chan_llm_semaphore()
        limit = self._img2chan_llm_limit()
        available = max(int(getattr(semaphore, "_value", limit) or 0), 0)
        active = max(limit - available, 0)
        waiters = getattr(semaphore, "_waiters", None)
        waiting = len(waiters) if waiters is not None else 0
        return active, waiting

    async def _call_img2chan_ollama_json(
        self,
        prompt: str,
        *,
        purpose: str,
        model: str,
        **kwargs,
    ) -> dict[str, Any]:
        semaphore = self._get_img2chan_llm_semaphore()
        active_before, waiting_before = self._img2chan_llm_queue_snapshot()
        if active_before > 0 or waiting_before > 0:
            log.info(
                "img2chan llm queued purpose=%s model=%s active=%d waiting=%d",
                purpose,
                model,
                active_before,
                waiting_before,
            )
        async with semaphore:
            active_now, waiting_now = self._img2chan_llm_queue_snapshot()
            log.info(
                "img2chan llm start purpose=%s model=%s active=%d waiting=%d",
                purpose,
                model,
                active_now,
                waiting_now,
            )
            return await call_ollama_json(
                prompt,
                model=model,
                **kwargs,
            )

    def _img2chan_own_post_feedback_milestones(self) -> tuple[int, ...]:
        raw = str(cfg("IMG2CHAN_OWN_POST_FEEDBACK_MILESTONES", "1,3,5,10") or "1,3,5,10")
        values: list[int] = []
        for token in raw.split(","):
            with contextlib.suppress(Exception):
                milestone = int(str(token or "").strip())
                if milestone > 0:
                    values.append(milestone)
        if not values:
            return (1, 3, 5, 10)
        return tuple(sorted(set(values)))

    def _clean_img2chan_learning_text(self, text: object, *, max_chars: int = 140) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"https?://\S+", "", value)
        value = re.sub(r"\s+", " ", value).strip(" 　-・>「」\"'")
        if not value:
            return ""
        return truncate_text(value, max(max_chars, 20)).strip(" 　-・>「」\"'")

    def _img2chan_feedback_importance(
        self,
        *,
        soudane: int,
        confidence: float = 0.6,
        own_post: bool = False,
    ) -> float:
        count = max(int(soudane or 0), 0)
        conf = max(0.0, min(float(confidence or 0.0), 1.0))
        score = 0.50 + min(count, 8) * 0.05 + max(conf - 0.5, 0.0) * 0.20
        if own_post:
            score += 0.05
        return max(0.0, min(score, 0.95))

    def _format_img2chan_learning_header(self, post: dict[str, Any]) -> str:
        reply_no = int(post.get("number") or 0)
        post_no = str(post.get("post_no") or "").strip()
        parts: list[str] = []
        if reply_no > 0:
            parts.append(f">>{reply_no}")
        if post_no:
            parts.append(f"No.{post_no}")
        return " ".join(parts) or "(番号不明)"

    def _get_img2chan_learning_context_chain(
        self,
        thread: dict[str, Any],
        reply: dict[str, Any],
        *,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        helper = _get_img2chan_helper()
        build_context_chain = getattr(helper, "build_img_thread_reply_context_chain", None) if helper is not None else None
        if not callable(build_context_chain):
            return []
        with contextlib.suppress(Exception):
            return [dict(item) for item in list(build_context_chain(thread, reply, max_depth=max_depth) or []) if isinstance(item, dict)]
        return []

    def _img2chan_learning_source_is_safe(
        self,
        reply: dict[str, Any],
        *,
        context_chain: list[dict[str, Any]] | None = None,
    ) -> bool:
        texts = [str(reply.get("text") or "").strip()]
        for context_post in list(context_chain or [])[:2]:
            texts.append(str(context_post.get("text") or "").strip())
        combined = "\n".join(text for text in texts if text)
        if not combined:
            return False
        return not bool(_IMG2CHAN_LEARNING_SOURCE_BLOCK_PATTERNS.search(combined))

    def _img2chan_learning_output_is_usable(
        self,
        *,
        pattern_summary: str,
        future_hint: str,
        avoid_hint: str,
    ) -> bool:
        main_parts = [part for part in (pattern_summary, future_hint) if part]
        if not main_parts:
            return False
        if all(_IMG2CHAN_LEARNING_PLACEHOLDER_PATTERNS.fullmatch(part) for part in main_parts):
            return False
        joined = " / ".join(part for part in (pattern_summary, future_hint, avoid_hint) if part)
        if _IMG2CHAN_LEARNING_GENERIC_OUTPUT_PATTERNS.search(joined):
            return False
        return True

    def _build_img2chan_soudane_analysis_prompt(
        self,
        *,
        url: str,
        thread: dict[str, Any],
        reply: dict[str, Any],
        own_post: bool,
        soudane: int,
        context_chain: list[dict[str, Any]] | None = None,
    ) -> str:
        op_text = self._clean_img_thread_fragment(thread.get("op_text"), max_chars=160) or "(本文なし)"
        reply_text = truncate_text(str(reply.get("text") or "").strip() or "(本文なし)", 280)
        context_lines: list[str] = []
        for index, context_post in enumerate(list(context_chain or [])):
            label = "直前の引用元" if index == 0 else "さらに前の引用元"
            context_lines.extend([
                f"{label}: {self._format_img2chan_learning_header(context_post)}",
                truncate_text(str(context_post.get('text') or '').strip() or "(本文なし)", 220),
            ])

        if not context_lines:
            context_lines.append("引用元の流れ: (抽出できず)")

        target_kind = "自分のレス" if own_post else "他ユーザーのレス"
        return "\n".join([
            "以下は img.2chan.net のスレ観測ログです。",
            "高評価レスから、今後の返しに活かせる短い学習メモだけを抽出してください。",
            "下品さ、危険さ、露悪さ、人格攻撃を強める方向は学習対象にしないでください。",
            "一回限りの固有ネタではなく、今後も転用できる返し方だけを残してください。",
            "『安心する』『共感を示す』『会話を円滑に進める』のような接客的な一般論には逃げないでください。",
            "匿名掲示板のレスとして見えている言い回しや切り返しの型だけを抽出してください。",
            "",
            f"[URL]\n{url}",
            f"[OP]\n{op_text}",
            f"[対象種別]\n{target_kind}",
            f"[そうだね]\n{soudane}",
            f"[対象レス]\n{self._format_img2chan_learning_header(reply)}\n{reply_text}",
            "",
            "[引用の流れ]",
            *context_lines,
            "",
            "[出力ルール]",
            "- should_store は、今後の返答改善に本当に使える時だけ true。",
            "- memory_type は communication_style / behavioral_principle / conversation_theme のいずれか。",
            "- pattern_summary は、何が受けたかを一文で一般化する。",
            "- future_hint は、次にどう応用するかを一文で書く。",
            "- avoid_hint は、似た場面で避けたい外し方があれば短く書く。なければ空文字。",
            "- 固有名詞やその場限りの文脈に寄りすぎない。",
        ])

    async def _store_img2chan_own_soudane_episode(
        self,
        message: discord.Message,
        *,
        url: str,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
        reply: dict[str, Any],
        previous_soudane: int,
        current_soudane: int,
    ) -> None:
        if current_soudane <= previous_soudane or current_soudane <= 0:
            return

        raw_milestones = thread_state.get("own_post_feedback_milestones")
        milestone_state: dict[Any, Any] = raw_milestones if isinstance(raw_milestones, dict) else {}
        thread_state["own_post_feedback_milestones"] = milestone_state

        post_no = str(reply.get("post_no") or "").strip()
        if not post_no:
            return

        last_recorded = int(milestone_state.get(post_no, 0) or 0)
        crossed = [
            milestone
            for milestone in self._img2chan_own_post_feedback_milestones()
            if milestone > last_recorded and current_soudane >= milestone
        ]
        if not crossed:
            return
        milestone_state[post_no] = max(crossed)

        add_episode = getattr(self.memory_store, "add_episode", None)
        if not callable(add_episode):
            return

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip() or "default"
        guild_id = getattr(message.guild, "id", None)
        op_text = self._clean_img_thread_fragment(thread.get("op_text"), max_chars=120) or "(本文なし)"
        reply_text = self._clean_img2chan_learning_text(reply.get("text"), max_chars=180) or "(本文なし)"
        previous_text = max(previous_soudane, 0)
        current_text = max(current_soudane, 0)
        importance = self._img2chan_feedback_importance(
            soudane=current_soudane,
            confidence=0.7,
            own_post=True,
        )

        try:
            await add_episode(
                persona=persona,
                guild_id=guild_id,
                channel_id=None,
                user_id=None,
                summary=f"imgで自分のレスがそうだね{current_text}まで伸びた。",
                what=f"OP: {op_text} / URL: {url}",
                bot_action=reply_text,
                user_reaction=f"そうだね {previous_text} -> {current_text}",
                importance=importance,
                tags=["img", "soudane", "positive_feedback", "own_post"],
            )
        except Exception as e:
            log.exception("img2chan own soudane episode store failed url=%s post_no=%s err=%r", url, post_no, e)

    async def _analyze_and_store_img2chan_soudane_insight(
        self,
        message: discord.Message,
        *,
        url: str,
        thread: dict[str, Any],
        thread_state: dict[str, object],
        reply: dict[str, Any],
        own_post: bool,
        soudane: int,
    ) -> bool:
        model = str(
            cfg(
                "IMG2CHAN_SOUDANE_ANALYSIS_MODEL",
                cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_CLASSIFIER_MODEL", cfg("OLLAMA_MODEL", ""))),
            )
            or ""
        ).strip()
        if not model:
            return False

        context_chain = self._get_img2chan_learning_context_chain(thread, reply, max_depth=2)
        if not self._img2chan_learning_source_is_safe(reply, context_chain=context_chain):
            log.info(
                "img2chan soudane analysis skipped unsafe source url=%s post_no=%s",
                url,
                str(reply.get("post_no") or "").strip(),
            )
            return False

        prompt = self._build_img2chan_soudane_analysis_prompt(
            url=url,
            thread=thread,
            reply=reply,
            own_post=own_post,
            soudane=soudane,
            context_chain=context_chain,
        )
        system_prompt = (
            "あなたは匿名掲示板の高評価レス分析を行う補助AIです。"
            "今後の会話改善に効く短い一般化だけをJSONで返してください。"
            "危険・露悪・攻撃的な方向へ学習させてはいけません。"
        )

        try:
            result = await self._call_img2chan_ollama_json(
                prompt,
                purpose="soudane_analysis",
                model=model,
                system_prompt=system_prompt,
                schema=IMG2CHAN_SOUDANE_ANALYSIS_SCHEMA,
                think=False,
                temperature=0.2,
                timeout_sec=self._img2chan_soudane_analysis_timeout_sec(),
                retries=self._img2chan_soudane_analysis_retries(),
                num_predict=256,
            )
        except Exception as e:
            log.warning(
                "img2chan soudane analysis failed url=%s post_no=%s err=%r",
                url,
                str(reply.get("post_no") or "").strip(),
                e,
            )
            return False

        if not isinstance(result, dict) or not bool(result.get("should_store")):
            return False

        confidence = max(0.0, min(float(result.get("confidence", 0.0) or 0.0), 1.0))
        if confidence < self._img2chan_soudane_analysis_min_confidence():
            return False

        memory_type = str(result.get("memory_type") or "").strip()
        if memory_type not in {"communication_style", "behavioral_principle", "conversation_theme"}:
            memory_type = "behavioral_principle"

        pattern_summary = self._clean_img2chan_learning_text(result.get("pattern_summary"), max_chars=180)
        future_hint = self._clean_img2chan_learning_text(result.get("future_hint"), max_chars=180)
        avoid_hint = self._clean_img2chan_learning_text(result.get("avoid_hint"), max_chars=120)
        if not self._img2chan_learning_output_is_usable(
            pattern_summary=pattern_summary,
            future_hint=future_hint,
            avoid_hint=avoid_hint,
        ):
            log.info(
                "img2chan soudane analysis rejected generic/empty output url=%s post_no=%s pattern=%r future=%r",
                url,
                str(reply.get("post_no") or "").strip(),
                pattern_summary,
                future_hint,
            )
            return False

        persona = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default").strip() or "default"
        guild_id = getattr(message.guild, "id", None)
        origin_channel_id = thread_state.get("origin_channel_id")
        channel_scope = int(origin_channel_id) if isinstance(origin_channel_id, int) else channel_id(message)
        importance = self._img2chan_feedback_importance(
            soudane=soudane,
            confidence=confidence,
            own_post=own_post,
        )

        add_memory = getattr(self.memory_store, "add_memory", None)
        if callable(add_memory):
            prefix = "imgで自分のレスが受けた型" if own_post else "imgで受けやすい返し方"
            content_parts = [f"{prefix}: {pattern_summary or future_hint}"]
            if future_hint:
                content_parts.append(f"次に活かすなら {future_hint}")
            if avoid_hint:
                content_parts.append(f"外しやすいのは {avoid_hint}")
            try:
                await add_memory(
                    persona=persona,
                    guild_id=guild_id,
                    channel_id=channel_scope,
                    user_id=None,
                    memory_type=memory_type,
                    content=" / ".join(part for part in content_parts if part),
                    score=importance,
                )
            except Exception as e:
                log.exception(
                    "img2chan soudane memory store failed url=%s post_no=%s err=%r",
                    url,
                    str(reply.get("post_no") or "").strip(),
                    e,
                )

        add_reflection = getattr(self.memory_store, "add_reflection", None)
        if callable(add_reflection):
            belief_seed = future_hint or pattern_summary
            belief = self._clean_img2chan_learning_text(
                f"imgでは、{belief_seed}" if belief_seed and not str(belief_seed).startswith("img") else belief_seed,
                max_chars=220,
            )
            if belief:
                try:
                    await add_reflection(
                        persona=persona,
                        guild_id=guild_id,
                        channel_id=None,
                        subject_kind="agent",
                        subject_key="core",
                        concept="img高評価パターン",
                        belief=belief,
                        importance_score=importance,
                        evidence_count=max(int(soudane), 1),
                        source_window_start=None,
                        source_window_end=None,
                    )
                except Exception as e:
                    log.exception(
                        "img2chan soudane reflection store failed url=%s post_no=%s err=%r",
                        url,
                        str(reply.get("post_no") or "").strip(),
                        e,
                    )

        return True

    async def _maybe_learn_from_img2chan_thread_feedback(
        self,
        message: discord.Message,
        url: str,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> None:
        if not self._img2chan_soudane_learning_enabled():
            return
        if not hasattr(self, "memory_store") or not self.memory_store:
            return

        raw_tracked = thread_state.get("soudane_by_post_no")
        tracked: dict[Any, Any] = raw_tracked if isinstance(raw_tracked, dict) else {}
        thread_state["soudane_by_post_no"] = tracked
        raw_analyzed = thread_state.get("analyzed_soudane_by_post_no")
        analyzed: dict[Any, Any] = raw_analyzed if isinstance(raw_analyzed, dict) else {}
        thread_state["analyzed_soudane_by_post_no"] = analyzed
        raw_own_nos = thread_state.get("own_post_nos")
        own_post_nos: set[Any] = raw_own_nos if isinstance(raw_own_nos, set) else set()
        thread_state["own_post_nos"] = own_post_nos

        candidates: list[dict[str, Any]] = []
        min_soudane = self._img2chan_soudane_learning_min_soudane()
        reanalyze_delta = self._img2chan_soudane_learning_reanalyze_delta()

        for reply in list(thread.get("replies") or []):
            if not isinstance(reply, dict):
                continue
            post_no = str(reply.get("post_no") or "").strip()
            if not post_no:
                continue
            current_soudane = max(int(reply.get("soudane") or 0), 0)
            previous_soudane = max(int(tracked.get(post_no, 0) or 0), 0)
            tracked[post_no] = current_soudane

            is_own_post = post_no in own_post_nos
            if is_own_post:
                await self._store_img2chan_own_soudane_episode(
                    message,
                    url=url,
                    thread=thread,
                    thread_state=thread_state,
                    reply=reply,
                    previous_soudane=previous_soudane,
                    current_soudane=current_soudane,
                )

            last_analyzed = max(int(analyzed.get(post_no, 0) or 0), 0)
            if current_soudane < min_soudane:
                continue
            if last_analyzed > 0 and current_soudane < last_analyzed + reanalyze_delta:
                continue
            candidates.append({
                "reply": dict(reply),
                "own_post": is_own_post,
                "soudane": current_soudane,
            })

        if not candidates:
            return

        candidates.sort(
            key=lambda item: (
                0 if bool(item.get("own_post")) else 1,
                -int(item.get("soudane") or 0),
                -int((item.get("reply") or {}).get("number") or 0),
            )
        )

        for candidate in candidates[:self._img2chan_soudane_learning_max_posts_per_poll()]:
            reply = dict(candidate.get("reply") or {})
            post_no = str(reply.get("post_no") or "").strip()
            if not post_no:
                continue
            soudane = max(int(candidate.get("soudane") or 0), 0)
            try:
                await self._analyze_and_store_img2chan_soudane_insight(
                    message,
                    url=url,
                    thread=thread,
                    thread_state=thread_state,
                    reply=reply,
                    own_post=bool(candidate.get("own_post")),
                    soudane=soudane,
                )
            finally:
                analyzed[post_no] = soudane

