from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from lib.text_utils import truncate_lines, truncate_text

from ..json_compat import dumps as json_dumps
from harness.agent.prompt_parser.parser.salvage import _strip_think_blocks
from .resolve import resolve_cfg

log = logging.getLogger("ollama_bot.common.ollama_helpers.text")

_cfg = resolve_cfg



def _mask_text_for_log(text: str, *, max_len: int | None = None) -> str:
    value = "" if text is None else str(text)
    resolved_max_len = max_len
    if resolved_max_len is None:
        try:
            resolved_max_len = int(
                _cfg(
                    "OLLAMA_LOG_PAYLOAD_MAX_CHARS",
                    _cfg("OLLAMA_LOG_PROMPT_MAX_CHARS", 0),
                )
                or 0
            )
        except Exception:
            resolved_max_len = 0

    masked = value
    masked = re.sub(r"\b\d{15,21}\b", "<id>", masked)
    masked = re.sub(
        r"data:[^;]+;base64,[A-Za-z0-9+/=]+",
        "<data-uri-base64>",
        masked,
        flags=re.IGNORECASE,
    )
    masked = re.sub(
        r"https?://([A-Za-z0-9._:-]+)(/[^\s\"'\)\]>\\]*)?",
        r"https://\1/<omitted>",
        masked,
        flags=re.IGNORECASE,
    )

    if resolved_max_len is not None and resolved_max_len > 0:
        masked = truncate_text(masked, resolved_max_len)

    return masked


def _log_ollama_payload(payload: dict[str, Any], *, kind: str) -> None:
    try:
        payload_text = json_dumps(payload, ensure_ascii=False)
    except Exception:
        payload_text = repr(payload)

    payload_hash = hashlib.sha256(payload_text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    payload_preview = _mask_text_for_log(payload_text)
    log.info("ollama %s payload sha256=%s payload=%s", kind, payload_hash, payload_preview)


def _looks_like_think_only_output(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw or not re.search(r"<think\b", raw, flags=re.IGNORECASE):
        return False
    if not re.search(r"</(?:think|thought)\b", raw, flags=re.IGNORECASE):
        return True
    return not _strip_think_blocks(raw).strip()


def format_ollama_metrics(model: str, data: dict[str, Any]) -> str:
    """Ollama API レスポンスから詳細メトリクス文字列を生成する。"""
    load_ns = float(data.get("load_duration") or 0)
    prompt_eval_ns = float(data.get("prompt_eval_duration") or 0)
    prompt_eval_count = int(data.get("prompt_eval_count") or 0)
    eval_ns = float(data.get("eval_duration") or 0)
    eval_count = int(data.get("eval_count") or 0)

    load_s = load_ns / 1e9
    prompt_eval_s = prompt_eval_ns / 1e9
    eval_s = eval_ns / 1e9
    eval_speed = (eval_count / eval_s) if eval_s > 0 else 0.0

    return (
        f"[ollama-metrics] model={model} "
        f"load={load_s:.1f}s "
        f"prompt_eval={prompt_eval_s:.1f}s({prompt_eval_count} tokens) "
        f"eval={eval_s:.1f}s({eval_count} tokens, {eval_speed:.1f} t/s)"
    )


def log_ollama_metrics(logger: logging.Logger, model: str, data: dict[str, Any]) -> None:
    """Ollama API レスポンスにメトリクスが含まれている場合、INFO レベルで出力する。"""
    if any(k in data for k in ("total_duration", "load_duration", "prompt_eval_duration", "eval_duration", "eval_count")):
        logger.info(format_ollama_metrics(model, data))


_log_ollama_metrics = log_ollama_metrics

