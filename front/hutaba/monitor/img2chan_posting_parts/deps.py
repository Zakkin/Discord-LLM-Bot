"""img2chan_posting 分割パーツ共通の依存インポート、フォールバック、型プロトコル定義。"""
from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
from dataclasses import dataclass, field
import difflib
import inspect
import random
import re
import time
import unicodedata
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import discord

from ollama_bot.common.discord_helpers import author_id, channel_id, content
from harness.agent.memory_manager.logic import get_relevant_memories, get_user_habit_profile_lines
from ollama_bot.common.ollama_helpers import (
    extract_first_user_facing_reply,
    looks_like_abnormal_assistant_reply,
    looks_like_parrot_reply,
    sanitize_generated_reply,
    truncate_text,
)
from ollama_bot.ollama_chat_.ollama_chat_helpers import _get_channel_kind
from front.hutaba.monitor.img2chan_context import (
    _get_img2chan_helper,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    log,
)
from front.hutaba.utils import _IMG_THREAD_URL_PATTERN, normalize_img_thread_url
from lib.config_utils import cfg_persona_namespace as _cfg_persona_namespace
from lib.text_utils import compact_exception_message as _compact_exception_message

try:
    from front.hutaba.client.img2chan import _normalize_img_post_no  # type: ignore[attr-defined]
except Exception:
    def _normalize_img_post_no(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"No\.(\d+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"(?<!\d)(\d{6,})(?!\d)", text)
        return match.group(1) if match else ""

try:
    from harness.agent.prompt_parser.prompt_manager.budget import sanitize_memory_candidates
except Exception:
    def sanitize_memory_candidates(memory_lines: list[str], last_assistant_text: str = "") -> list[str]:
        return [str(line).strip() for line in memory_lines if str(line).strip()]

try:
    from harness.agent.prompt_parser.parser.validation import (
        find_persona_tone_loss,
        persona_tone_reason_message,
    )
except ImportError:
    # harness 側の実装が無い環境でも単体動作するフォールバック（口調検査は無効化）
    def find_persona_tone_loss(
        body: str,
        *,
        first_person: str = "",
        exempt_max_chars: int = 20,
        markers: Iterable[str] | None = None,
    ) -> str | None:
        return None

    def persona_tone_reason_message(code: str | None) -> str:
        return str(code or "")

try:
    from harness.agent.tool_registry.web_research.helpers import extract_named_term_candidates
except Exception:
    extract_named_term_candidates = None  # type: ignore[assignment]

try:
    from harness.agent.memory_manager.logic import (
        SlangEntry,
        filter_query_terms_by_slang,
        format_slang_guidance_for_prompt,
        lookup_slang_in_text,
    )
except (ImportError, AttributeError):
    # 他モジュール未完了時・単体テスト用のセーフフォールバック
    _LOCAL_PREFIX_SLANG = {"すみフラ", "うろフラ", "フラ"}
    SlangEntry = dict[str, Any]  # type: ignore[misc,assignment]

    def lookup_slang_in_text(text: str) -> list[dict[str, Any]]:  # type: ignore[misc]
        matched = []
        for term in _LOCAL_PREFIX_SLANG:
            if term in text:
                matched.append({
                    "term": term,
                    "meaning": "質問・相談の前置き略語",
                    "guidance": "キャラクター名や作品名ではなく、質問・相談の前置き略語です。",
                    "strip_from_query": True,
                })
        return matched

    def filter_query_terms_by_slang(terms: list[str]) -> list[str]:
        return [t for t in terms if t.strip() not in _LOCAL_PREFIX_SLANG and not any(p in t for p in ("すみフラ", "うろフラ"))]

    def format_slang_guidance_for_prompt(entries: list[dict[str, Any]]) -> list[str]:  # type: ignore[misc]
        return [f"- 『{e['term']}』: {e.get('guidance', e.get('meaning', ''))}" for e in entries if "term" in e]

try:
    from harness.agent.hooks import POST_PRE_SEND, HookContext, run_hooks
    _HOOKS_AVAILABLE = True
except Exception:  # pragma: no cover - フック基盤が無い環境では無効化
    _HOOKS_AVAILABLE = False

try:
    import harness.agent.prompt_parser.prompt_manager.discussed_topics as _discussed_topics
    discussed_topics: Any = _discussed_topics
except Exception:
    discussed_topics = None

lessons: Any
try:
    import harness.agent.prompt_parser.prompt_manager.lessons as _lessons
    lessons = _lessons
except Exception:
    lessons = None

find_ungrounded_image_claims: Any
try:
    from harness.agent.prompt_parser.parser.image_claims import (
        find_ungrounded_image_claims as _find_ungrounded_image_claims,
    )
    find_ungrounded_image_claims = _find_ungrounded_image_claims
except Exception:
    find_ungrounded_image_claims = None

_build_video_caution_note: Any
_collect_self_names: Any
_is_self_name_guard_enabled: Any
_is_video_caution_enabled: Any
_strip_self_name_vocative: Any
try:
    from front.hutaba.monitor.img2chan_reply_guards import (
        build_video_caution_note as _build_video_caution_note_impl,
        collect_self_names as _collect_self_names_impl,
        is_self_name_guard_enabled as _is_self_name_guard_enabled_impl,
        is_video_caution_enabled as _is_video_caution_enabled_impl,
        strip_self_name_vocative as _strip_self_name_vocative_impl,
    )
    _build_video_caution_note = _build_video_caution_note_impl
    _collect_self_names = _collect_self_names_impl
    _is_self_name_guard_enabled = _is_self_name_guard_enabled_impl
    _is_video_caution_enabled = _is_video_caution_enabled_impl
    _strip_self_name_vocative = _strip_self_name_vocative_impl
except Exception:
    _build_video_caution_note = None
    _collect_self_names = None
    _is_self_name_guard_enabled = None
    _is_video_caution_enabled = None
    _strip_self_name_vocative = None

from front.hutaba.monitor.img2chan_candidate_compare import (
    append_hutaba_candidate,
    build_hutaba_compare_context,
    record_hutaba_candidate_compare,
    resolve_hutaba_candidate_count,
    select_best_hutaba_candidate,
)

_is_quoted_image_priority_enabled: Any
_order_context_image_candidates: Any
try:
    from front.hutaba.monitor.img2chan_image_priority import (
        is_quoted_image_priority_enabled as _is_quoted_image_priority_enabled_impl,
        order_context_image_candidates as _order_context_image_candidates_impl,
    )
    _is_quoted_image_priority_enabled = _is_quoted_image_priority_enabled_impl
    _order_context_image_candidates = _order_context_image_candidates_impl
except Exception:
    _is_quoted_image_priority_enabled = None
    _order_context_image_candidates = None


@dataclass
class _HutabaReplyPromptContext:
    """レス生成前のスレッド文脈・記憶・プロンプト構築結果を保持する構造体。"""

    prompt: str
    system_prompt: str
    posts: list[dict[str, Any]]
    thread_terms: set[str]
    frequent_terms: dict[str, int]
    persona_names: set[str]
    attached_image_description: str
    memory_store: Any


@dataclass
class _CandidateLoopState:
    """候補生成・ガード検証ループを跨いで引き継ぐ可変状態。"""

    comment: str = ""
    last_candidate_comment: str = ""
    last_ungrounded_names: list[str] = field(default_factory=list)
    last_premise_violations: list[str] = field(default_factory=list)
    effective_target_reply: dict[str, Any] | None = None
    last_generation_error: Exception | None = None
    image_claim_retried: bool = False
    compare_candidates: list[dict[str, Any]] = field(default_factory=list)
    last_image_claim_csv: str = ""
    last_discussed_topic_match: dict[str, Any] | None = None
    fallback_quote_posts: list[str] = field(default_factory=list)
    fallback_quote_replies: list[int] = field(default_factory=list)


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol

    class _Img2chanPostingBase(OllamaChatProtocol):
        """分割された各投稿 Mixin 間で参照し合うメソッドの静的型宣言。"""

        def _img2chan_proactive_post_enabled(self) -> bool: ...
        def _img2chan_proactive_post_interval_min_sec(self) -> float: ...
        def _img2chan_proactive_post_interval_max_sec(self) -> float: ...
        def _img2chan_autonomous_followup_cooldown_sec(self) -> float: ...
        def _is_discord_user_reply_busy(self) -> bool: ...
        async def _wait_for_discord_user_replies_idle(self, timeout_sec: float = 30.0) -> None: ...
        async def _yield_to_user_replies(self, timeout_sec: float = 30.0) -> None: ...
        def _img2chan_exclude_stream_threads(self) -> bool: ...
        def _is_img2chan_stream_or_live_thread(self, thread_item: dict[str, Any]) -> str | None: ...
        def _img2chan_exclude_request_threads(self) -> bool: ...
        def _is_img2chan_request_thread(self, thread_item: dict[str, Any]) -> str | None: ...
        def _is_hutaba_thread_expiring_soon(
            self,
            thread_or_item: dict[str, Any] | None,
            *,
            min_remaining_sec: float | None = None,
        ) -> str | None: ...
        def _is_img2chan_thread_vandalized(self, url: str) -> bool: ...
        def _mark_img2chan_thread_vandalized(self, url: str, reason: str = "") -> None: ...
        def _save_img2chan_checkpoint(self, thread_state: dict[str, Any], **kwargs: Any) -> None: ...
        def _restore_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> dict[str, Any] | None: ...
        def _clear_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> None: ...
        def _detect_img2chan_thread_vandalism(
            self,
            thread: dict[str, Any],
            thread_state: dict[str, Any],
        ) -> str | None: ...
        def _sanitize_hutaba_reply_text(self, text: str, max_chars: int = 120) -> str: ...
        def _img2chan_character_guidance(self) -> str: ...
        def _img2chan_post_delay_sec(self) -> float: ...
        def _img2chan_post_generation_retries(self) -> int: ...
        def _img2chan_post_generation_models(self) -> list[str]: ...
        def _log_img2chan_thread_analysis(
            self,
            *,
            context: str,
            url: str,
            thread: dict[str, Any],
            message: discord.Message | None = None,
        ) -> None: ...
        def _mark_img2chan_promotion_sent(self) -> None: ...
        def _maybe_append_img2chan_promotion(
            self,
            helper: Any,
            comment: str,
        ) -> tuple[str, bool, str]: ...
        def _build_img2chan_followup_retry_prompt(
            self,
            base_prompt: str,
            *,
            target_reply: dict[str, Any] | None = None,
        ) -> str: ...
        def _img2chan_comment_parrot_reason(
            self,
            arg1: Any = None,
            arg2: Any = None,
            *,
            target_reply: dict[str, Any] | None = None,
            quoted_own_posts: list[dict[str, Any]] | None = None,
            thread: dict[str, Any] | None = None,
        ) -> Any: ...
        def _extract_early_thread_context_lines(
            self,
            posts: list[dict[str, Any]],
            *,
            max_lines: int = 6,
        ) -> list[str]: ...
        def _extract_hutaba_target_context_lines(
            self,
            thread: dict[str, Any],
            target_reply: dict[str, Any],
            posts: list[dict[str, Any]],
            *,
            max_lines: int = 6,
        ) -> list[str]: ...
        def _resolve_img2chan_quoted_target_reply(
            self,
            thread: dict[str, Any],
            quote_post_numbers: list[str] | None = None,
            quote_reply_numbers: list[int] | None = None,
        ) -> dict[str, Any] | None: ...
        def _extract_img2chan_macro_topic_summary(
            self,
            thread: dict[str, Any],
            posts: list[dict[str, Any]],
        ) -> str: ...
        async def _resolve_hutaba_thread_topic_context(
            self,
            thread: dict[str, Any],
            thread_state: dict[str, Any],
            *,
            target_reply: dict[str, Any] | None = None,
            posts: list[dict[str, Any]] | None = None,
        ) -> str: ...
        async def _prepare_hutaba_reply_prompt_context(
            self,
            message: discord.Message | None,
            url: str,
            *,
            helper: Any,
            max_chars: int,
            character_guidance: str,
            thread: dict[str, Any],
            thread_state: dict[str, Any],
            target_reply: dict[str, Any] | None = None,
            quoted_own_posts: list[dict[str, Any]] | None = None,
            requester_text: str = "",
        ) -> _HutabaReplyPromptContext: ...
        def _check_hutaba_pre_quote_guards(
            self,
            *,
            url: str,
            generation_model: str,
            prompt: str,
            prompt_text: str,
            prompt_variant: str,
            prompt_variants: list[tuple[str, str]],
            variant_idx: int,
            candidate_text: str,
            parsed_thought: str,
            max_chars: int,
            attached_image_description: str,
            compare_enabled: bool,
            candidate_target_count: int,
            state: _CandidateLoopState,
        ) -> tuple[str, str]: ...
        async def _check_hutaba_post_quote_guards(
            self,
            *,
            message: discord.Message | None,
            url: str,
            helper: Any,
            thread: dict[str, Any],
            thread_state: dict[str, Any],
            target_reply: dict[str, Any] | None,
            quoted_own_posts: list[dict[str, Any]] | None,
            generation_model: str,
            prompt: str,
            retry_prompt: str,
            prompt_variant: str,
            prompt_variants: list[tuple[str, str]],
            variant_idx: int,
            cleaned_comment: str,
            quote_post_numbers: list[str],
            quote_reply_numbers: list[int],
            thread_terms: set[str],
            frequent_terms: dict[str, int],
            persona_names: set[str],
            memory_store: Any,
            compare_enabled: bool,
            candidate_target_count: int,
            state: _CandidateLoopState,
        ) -> str: ...
        async def _generate_hutaba_reply(
            self,
            message: discord.Message | None,
            url: str,
            *,
            thread: dict[str, Any],
            thread_state: dict[str, Any],
            target_reply: dict[str, Any] | None = None,
            quoted_own_posts: list[dict[str, Any]] | None = None,
            requester_text: str = "",
        ) -> tuple[str, list[str], list[int]]: ...
        async def _generate_hutaba_post_emotion_via_llm(
            self,
            *,
            url: str,
            thread: dict[str, Any] | None,
            comment: str,
            target_reply: dict[str, Any] | None = None,
        ) -> dict[str, Any] | None: ...
        async def _execute_img2chan_thread_post(
            self,
            message: discord.Message | None,
            url: str,
            *,
            delay_sec: float | None,
            thread_state: dict[str, Any] | None = None,
            requester_text: str = "",
            target_reply: dict[str, Any] | None = None,
            quoted_own_posts: list[dict[str, Any]] | None = None,
            report_result: bool = True,
        ) -> dict[str, Any]: ...
        async def _send_img2chan_post_report(
            self,
            message: discord.Message | None,
            post_result: dict[str, Any],
            *,
            delay_sec: float | None = None,
        ) -> None: ...
else:
    _Img2chanPostingBase = object
