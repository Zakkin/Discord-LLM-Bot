"""OllamaChatReplyCoreMixinの共通定数、プロキシ関数、例外定義。

このモジュールは各分割先Mixin（prompts, runtime, sending, episode, model_call, break）から
参照される共通のモジュールレベル定義を提供し、循環importを防止する。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sys
from typing import Any, Sequence, TYPE_CHECKING

from lib.dispatch_utils import get_public_attr
try:
    from lib.error_report import report_error as _default_report_error
except ImportError:
    def _default_report_error(site: str, exc: BaseException) -> None: pass
import discord

from ollama_bot.common.ollama_helpers import call_ollama_json as _orig_call_ollama_json
from ollama_bot.ollama_chat_ import ollama_chat_helpers as _helpers
from harness.agent.memory_manager.summarizer import (
    maybe_store_training_candidate as _default_maybe_store_training_candidate,
)
from harness.agent.prompt_parser.prompt_manager.user_interest_logic import (
    update_user_interests as _default_update_user_interests,
)
from harness.agent.memory_manager.logic import (
    extract_and_store_memory as _default_extract_and_store_memory,
    maybe_update_channel_summary as _default_maybe_update_channel_summary,
    update_user_habit_profile as _default_update_user_habit_profile,
)

try:
    import harness.agent.memory_manager.logic as _memory_logic
    _default_add_prospective = getattr(_memory_logic, "add_prospective", None)
    _default_list_pending_prospective_memories = getattr(_memory_logic, "list_pending_prospective_memories", None)
except ImportError:
    _default_add_prospective = None
    _default_list_pending_prospective_memories = None

log = logging.getLogger("ollama_bot.ollama_chat_.reply_core")


class ReplyTargetDeletedError(Exception):
    """返信対象メッセージが削除済みで、かつチャンネル投稿への切り替えを行わない場合に送出する。"""


_PUBLIC_REPLY_MODULE = "front.discord.reply.reply"
_REPLY_CORE_MODULE = "front.discord.reply.reply_core"

_default_cfg = _helpers.cfg
_default_cfg_int = _helpers.cfg_int
_default_cfg_float = _helpers.cfg_float
_default_cfg_bool = _helpers.cfg_bool
_default_cfg_managed_channel_ids = _helpers.cfg_managed_channel_ids
_default_fetch_recent_context_lines = _helpers.fetch_recent_context_lines
_default_find_last_assistant_message_text = _helpers._find_last_assistant_message_text
_default_fetch_recent_x_timeline_items = _helpers.fetch_recent_x_timeline_items
_default_research_dispatch = _helpers.research_dispatch
_default_extract_base64_images = _helpers.extract_base64_images
_default_build_message_media_context = _helpers.build_message_media_context
_default_resolve_reference_message = _helpers.resolve_reference_message
_default_analyze_pre_reply_context: Any = getattr(_helpers, "_analyze_pre_reply_context", None)
_default_build_user_prompt: Any = getattr(_helpers, "_build_user_prompt", None)
_default_call_ollama_chat_raw: Any = getattr(_helpers, "call_ollama_chat_raw", None)
_default_release_ollama_model: Any = None
try:
    from ollama_bot.common.ollama_helpers import release_ollama_model as _orig_release_model
    _default_release_ollama_model = _orig_release_model
except ImportError:
    pass

from front.discord.common.discord_helpers import (
    build_different_author_boundary_note,
    collect_same_author_burst_texts,
    format_context_lines_with_relation_labels,
    format_discord_context_line_with_relation,
    format_message_reactions,
    looks_like_incomplete_topic_opener,
    looks_like_meta_ignorance_reply,
    sanitize_meta_ignorance_reply,
)


def _public_attr(name: str, default: Any = None) -> Any:
    # 1. reply_core がモンキーパッチされていればそれを優先（テストの互換性維持）
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    if core_mod is not None:
        custom_public_attr = getattr(core_mod, "_public_attr", None)
        if custom_public_attr is not None and custom_public_attr is not _public_attr:
            return custom_public_attr(name, default)
        val = getattr(core_mod, name, None)
        if val is not None and val is not default:
            return val
    # 2. 公開リプライモジュール (front.discord.reply.reply)
    return get_public_attr(_PUBLIC_REPLY_MODULE, name, default)


def report_error(site: str, exc: BaseException) -> None:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    if core_mod is not None:
        fn = getattr(core_mod, "report_error", None)
        if callable(fn) and fn is not report_error:
            fn(site, exc)
            return
    _default_report_error(site, exc)


async def _analyze_pre_reply_context(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_analyze_pre_reply_context", _default_analyze_pre_reply_context)
    if not callable(fn):
        return None
    res = _default_analyze_pre_reply_context(*args, **kwargs) if fn is _analyze_pre_reply_context else fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


async def _build_user_prompt(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_build_user_prompt", _default_build_user_prompt)
    if not callable(fn):
        return ("", None, [])
    res = _default_build_user_prompt(*args, **kwargs) if fn is _build_user_prompt else fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


def call_ollama_chat_raw(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("call_ollama_chat_raw", _default_call_ollama_chat_raw)
    if not callable(fn):
        return {}
    return _default_call_ollama_chat_raw(*args, **kwargs) if fn is call_ollama_chat_raw else fn(*args, **kwargs)


def release_ollama_model(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("release_ollama_model", _default_release_ollama_model)
    if not callable(fn):
        return None
    return _default_release_ollama_model(*args, **kwargs) if fn is release_ollama_model else fn(*args, **kwargs)


def cfg(name: str, default: Any = None) -> Any:
    fn = _public_attr("cfg", _default_cfg)
    return _default_cfg(name, default) if fn is cfg else fn(name, default)


def cfg_int(name: str, default: int = 0) -> int:
    fn = _public_attr("cfg_int", _default_cfg_int)
    return _default_cfg_int(name, default) if fn is cfg_int else fn(name, default)


def cfg_float(name: str, default: float = 0.0) -> float:
    fn = _public_attr("cfg_float", _default_cfg_float)
    return _default_cfg_float(name, default) if fn is cfg_float else fn(name, default)


def cfg_bool(name: str, default: bool = False) -> bool:
    fn = _public_attr("cfg_bool", _default_cfg_bool)
    return _default_cfg_bool(name, default) if fn is cfg_bool else fn(name, default)


def cfg_managed_channel_ids() -> set[int]:
    fn = _public_attr("cfg_managed_channel_ids", _default_cfg_managed_channel_ids)
    return _default_cfg_managed_channel_ids() if fn is cfg_managed_channel_ids else fn()


def fetch_recent_context_lines(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("fetch_recent_context_lines", _default_fetch_recent_context_lines)
    return _default_fetch_recent_context_lines(*args, **kwargs) if fn is fetch_recent_context_lines else fn(*args, **kwargs)


def _find_last_assistant_message_text(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_find_last_assistant_message_text", _default_find_last_assistant_message_text)
    return _default_find_last_assistant_message_text(*args, **kwargs) if fn is _find_last_assistant_message_text else fn(*args, **kwargs)


def fetch_recent_x_timeline_items(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("fetch_recent_x_timeline_items", _default_fetch_recent_x_timeline_items)
    return _default_fetch_recent_x_timeline_items(*args, **kwargs) if fn is fetch_recent_x_timeline_items else fn(*args, **kwargs)


def research_dispatch(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("research_dispatch", _default_research_dispatch)
    return _default_research_dispatch(*args, **kwargs) if fn is research_dispatch else fn(*args, **kwargs)


def extract_base64_images(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("extract_base64_images", _default_extract_base64_images)
    return _default_extract_base64_images(*args, **kwargs) if fn is extract_base64_images else fn(*args, **kwargs)


def build_message_media_context(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("build_message_media_context", _default_build_message_media_context)
    return _default_build_message_media_context(*args, **kwargs) if fn is build_message_media_context else fn(*args, **kwargs)


def resolve_reference_message(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("resolve_reference_message", _default_resolve_reference_message)
    return _default_resolve_reference_message(*args, **kwargs) if fn is resolve_reference_message else fn(*args, **kwargs)


def extract_and_store_memory(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    default_fn = getattr(core_mod, "_default_extract_and_store_memory", _default_extract_and_store_memory) if core_mod is not None else _default_extract_and_store_memory
    fn = _public_attr("extract_and_store_memory", default_fn)
    return default_fn(*args, **kwargs) if fn is extract_and_store_memory else fn(*args, **kwargs)


def maybe_store_training_candidate(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    default_fn = getattr(core_mod, "_default_maybe_store_training_candidate", _default_maybe_store_training_candidate) if core_mod is not None else _default_maybe_store_training_candidate
    fn = _public_attr("maybe_store_training_candidate", default_fn)
    return default_fn(*args, **kwargs) if fn is maybe_store_training_candidate else fn(*args, **kwargs)


def update_user_habit_profile(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    default_fn = getattr(core_mod, "_default_update_user_habit_profile", _default_update_user_habit_profile) if core_mod is not None else _default_update_user_habit_profile
    fn = _public_attr("update_user_habit_profile", default_fn)
    return default_fn(*args, **kwargs) if fn is update_user_habit_profile else fn(*args, **kwargs)


def update_user_interests(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    default_fn = getattr(core_mod, "_default_update_user_interests", _default_update_user_interests) if core_mod is not None else _default_update_user_interests
    fn = _public_attr("update_user_interests", default_fn)
    return default_fn(*args, **kwargs) if fn is update_user_interests else fn(*args, **kwargs)


def _build_summary_context_lines(*args: Any, **kwargs: Any) -> Any:
    fn = _public_attr("_build_summary_context_lines", _helpers._build_summary_context_lines)
    return _helpers._build_summary_context_lines(*args, **kwargs) if fn is _build_summary_context_lines else fn(*args, **kwargs)


def maybe_update_channel_summary(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    default_fn = getattr(core_mod, "_default_maybe_update_channel_summary", _default_maybe_update_channel_summary) if core_mod is not None else _default_maybe_update_channel_summary
    fn = _public_attr("maybe_update_channel_summary", default_fn)
    return default_fn(*args, **kwargs) if fn is maybe_update_channel_summary else fn(*args, **kwargs)


def add_prospective(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    core_fn = getattr(core_mod, "add_prospective", None) if core_mod is not None else None
    if core_fn is not None and core_fn is not add_prospective:
        return core_fn(*args, **kwargs)
    fn = _public_attr("add_prospective", _default_add_prospective)
    if fn is not None and fn is not add_prospective:
        return fn(*args, **kwargs)
    if _default_add_prospective is not None:
        return _default_add_prospective(*args, **kwargs)
    if args and hasattr(args[0], "add_prospective"):
        return args[0].add_prospective(*args[1:], **kwargs)
    return None


def list_pending_prospective_memories(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    core_fn = getattr(core_mod, "list_pending_prospective_memories", None) if core_mod is not None else None
    if core_fn is not None and core_fn is not list_pending_prospective_memories:
        return core_fn(*args, **kwargs)
    fn = _public_attr("list_pending_prospective_memories", _default_list_pending_prospective_memories)
    if fn is not None and fn is not list_pending_prospective_memories:
        return fn(*args, **kwargs)
    if _default_list_pending_prospective_memories is not None:
        return _default_list_pending_prospective_memories(*args, **kwargs)
    if args and hasattr(args[0], "list_pending_prospective_memories"):
        return args[0].list_pending_prospective_memories(*args[1:], **kwargs)
    return []


def call_ollama_json(*args: Any, **kwargs: Any) -> Any:
    core_mod = sys.modules.get(_REPLY_CORE_MODULE)
    fn = getattr(core_mod, "call_ollama_json", None) if core_mod is not None else None
    if fn is not None and fn is not call_ollama_json:
        return fn(*args, **kwargs)
    return _orig_call_ollama_json(*args, **kwargs)


def _is_context_size_exceeded_error(exc: Exception) -> bool:
    """Ollamaのコンテキストサイズ超過（HTTP 400 Bad Request / exceed_context_size_error 等）かを判定する。"""
    try:
        from ollama_bot.common.ollama_helpers import OllamaContextSizeExceededError
        if isinstance(exc, OllamaContextSizeExceededError):
            return True
    except Exception:
        pass

    err_str = str(exc).lower()
    if any(marker in err_str for marker in (
        "exceed_context_size",
        "context length",
        "context size",
        "maximum context",
        "too many tokens",
    )):
        return True

    status = getattr(exc, "status", None)
    if status == 400:
        return True

    return False


def _is_transient_reply_model_error(exc: Exception) -> bool:
    """一時的なネットワーク／サーバー障害であり、待機後に再送すべきかを判定する。"""
    if _is_context_size_exceeded_error(exc):
        return False

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError)):
        return True

    try:
        import aiohttp
        if isinstance(exc, aiohttp.ClientResponseError):
            if 400 <= exc.status < 500 and exc.status not in (408, 429):
                return False
            if exc.status in (408, 429, 500, 502, 503, 504):
                return True
            return False
        if isinstance(exc, (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError, aiohttp.ClientPayloadError)):
            return True
    except Exception:
        pass

    status = getattr(exc, "status", None)
    if status is not None and isinstance(status, int):
        if 400 <= status < 500 and status not in (408, 429):
            return False
        if status in (408, 429, 500, 502, 503, 504):
            return True

    exc_type = type(exc)
    name = str(getattr(exc_type, "__name__", "") or "").lower()
    if "response" in name and "error" in name:
        return False
    if any(marker in name for marker in ("timeout", "socket", "serverdisconnected", "connection")):
        return True

    module = str(getattr(exc_type, "__module__", "") or "").lower()
    if "aiohttp" in module:
        return True
    return False


def _reply_model_timeout_fallback() -> str:
    fallback = str(cfg("OLLAMA_MODEL_TIMEOUT_FALLBACK", "") or "").strip()
    if fallback:
        return fallback
    fallback = str(
        cfg(
            "OLLAMA_EMPTY_REPLY_FALLBACK",
            "今ちょっと応答が重い。もう一回言ってくれ。",
        )
        or ""
    ).strip()
    return fallback or "今ちょっと応答が重い。もう一回言ってくれ。"


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import MessageRuntime, OllamaChatProtocol

    class _OllamaChatReplyCoreBase(OllamaChatProtocol):
        def _get_channel_context_queue(self, message: discord.Message | None) -> Sequence[Any] | None: ...
        def _calculate_message_elapsed_sec(self, message: discord.Message) -> float: ...
        def _calculate_channel_gap(self, message: discord.Message) -> int: ...
        def _context_max_age_sec(self) -> float: ...
        def _ensure_background_llm_serialization(self) -> None: ...
        def _clean_duplicate_prompt_instructions(self, prompt: str, system_prompt: str) -> str: ...
        def _resolve_author_display_name(self, message: Any) -> str: ...
        def _get_thread_context_for_working_memory(
            self, message: discord.Message, runtime: MessageRuntime, bot_identity: Any
        ) -> tuple[list[dict[str, Any]], str | None]: ...
        def _get_user_aliases_for_message(self, message: discord.Message) -> Any: ...
        def _apply_different_author_boundary_note(
            self, prompt: str, message: discord.Message, last_history_message: Any = None
        ) -> str: ...
        def _call_reply_model_or_fallback(
            self,
            message: discord.Message,
            prompt: str,
            *,
            system_prompt: str,
            images: list[str] | None,
            num_predict: int | None,
            think_override: bool | None = None,
            num_ctx: int | None = None,
        ) -> Any: ...
        def _trim_memory_context_lines(self, context_lines: list[str]) -> list[str]: ...
        def _reply_num_predict(
            self, *, intent_info: dict[str, Any] | None = None, prompt_text: str = "", system_prompt: str = ""
        ) -> int | None: ...
        def _sanitize_frustrated_apology(
            self, reply_text: str, *, is_correction: bool, anger_count: int
        ) -> str: ...
else:
    _OllamaChatReplyCoreBase = object


_AIZUCHI_PATTERN = re.compile(
    r"^(?:うん|うーん|そうだね|そうなんだ|なるほど|あはは|わはは|おっけー|オッケー|了解|わかる|それな|いいね|すごい|すごーい|へー|へぇ|ふーん|確かに|せやな|草|おつかれ|お疲れ|感謝|ありがとう|はい|ハーイ)[！!？?ー〜wW\s。]*$"
)


def _split_reply_text(text: str, max_chunks: int = 3) -> list[str]:
    """返信文を文末（。！？\n）を基準に最大 max_chunks 個に分割する純粋関数。

    - コードブロック（```...```）内および URL は途中で絶対に分割しない。
    - チャンク数が上限を超える場合は、末尾チャンクに余りを結合する。
    """
    cleaned = str(text or "").strip()
    if not cleaned or max_chunks <= 1:
        return [cleaned] if cleaned else []

    protected_spans: list[tuple[int, int]] = []

    # コードブロック: ``` ... ``` または ` ... `
    for m in re.finditer(r"```[\s\S]*?```|`[^`\n]+`", cleaned):
        protected_spans.append((m.start(), m.end()))

    # URL: https?://...
    for m in re.finditer(r"https?://[^\s<>\"']+", cleaned):
        protected_spans.append((m.start(), m.end()))

    def is_protected(pos: int) -> bool:
        return any(start <= pos < end for start, end in protected_spans)

    split_chars = set("。！？!?\n")
    split_indices: list[int] = []
    i = 0
    n = len(cleaned)

    while i < n:
        if is_protected(i):
            i += 1
            continue

        if cleaned[i] in split_chars:
            j = i
            while j < n and cleaned[j] in split_chars and not is_protected(j):
                j += 1
            split_indices.append(j)
            i = j
        else:
            i += 1

    if not split_indices:
        return [cleaned]

    chunks: list[str] = []
    prev = 0
    for idx in split_indices:
        segment = cleaned[prev:idx].strip()
        if segment:
            chunks.append(segment)
        prev = idx

    last_segment = cleaned[prev:].strip()
    if last_segment:
        chunks.append(last_segment)

    if not chunks:
        return [cleaned]

    if len(chunks) > max_chunks:
        head = chunks[: max_chunks - 1]
        tail = "".join(chunks[max_chunks - 1 :])
        return head + [tail]

    return chunks


__all__ = [
    "ReplyTargetDeletedError",
    "_PUBLIC_REPLY_MODULE",
    "_REPLY_CORE_MODULE",
    "_default_cfg",
    "_default_cfg_int",
    "_default_cfg_float",
    "_default_cfg_bool",
    "_default_cfg_managed_channel_ids",
    "_default_fetch_recent_context_lines",
    "_default_find_last_assistant_message_text",
    "_default_fetch_recent_x_timeline_items",
    "_default_research_dispatch",
    "_default_extract_base64_images",
    "_default_build_message_media_context",
    "_default_resolve_reference_message",
    "_default_extract_and_store_memory",
    "_default_maybe_store_training_candidate",
    "_default_update_user_habit_profile",
    "_default_update_user_interests",
    "_default_maybe_update_channel_summary",
    "_default_add_prospective",
    "_default_list_pending_prospective_memories",
    "_public_attr",
    "cfg",
    "cfg_int",
    "cfg_float",
    "cfg_bool",
    "cfg_managed_channel_ids",
    "fetch_recent_context_lines",
    "_find_last_assistant_message_text",
    "fetch_recent_x_timeline_items",
    "research_dispatch",
    "extract_base64_images",
    "build_message_media_context",
    "resolve_reference_message",
    "extract_and_store_memory",
    "maybe_store_training_candidate",
    "update_user_habit_profile",
    "update_user_interests",
    "_build_summary_context_lines",
    "maybe_update_channel_summary",
    "add_prospective",
    "list_pending_prospective_memories",
    "call_ollama_json",
    "_is_context_size_exceeded_error",
    "_is_transient_reply_model_error",
    "_reply_model_timeout_fallback",
    "_OllamaChatReplyCoreBase",
    "_AIZUCHI_PATTERN",
    "_split_reply_text",
    "report_error",
    "_analyze_pre_reply_context",
    "_build_user_prompt",
    "call_ollama_chat_raw",
    "release_ollama_model",
    "build_different_author_boundary_note",
    "collect_same_author_burst_texts",
    "format_context_lines_with_relation_labels",
    "format_discord_context_line_with_relation",
    "format_message_reactions",
    "looks_like_incomplete_topic_opener",
    "looks_like_meta_ignorance_reply",
    "sanitize_meta_ignorance_reply",
    "log",
]
