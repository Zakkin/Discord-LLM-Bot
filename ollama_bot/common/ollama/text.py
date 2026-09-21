from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from lib.text_utils import truncate_lines, truncate_text

from ..json_compat import dumps as json_dumps
from ..ollama_reply_safety import _strip_think_blocks
from .client import _cfg

log = logging.getLogger("ollama_bot.common.ollama.text")



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
        r"https?://([A-Za-z0-9._:-]+)(/[^\s]*)?",
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
