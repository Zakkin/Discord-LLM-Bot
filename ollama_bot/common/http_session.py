from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .json_compat import aiohttp_dumps

log = logging.getLogger("ollama_bot.common.http_session")

_shared_http_session: Optional[aiohttp.ClientSession] = None
_shared_http_session_lock: Optional[asyncio.Lock] = None


def _build_timeout(timeout_sec: float | None = None) -> aiohttp.ClientTimeout:
    resolved = float(timeout_sec or 30.0)
    return aiohttp.ClientTimeout(
        total=resolved,
        sock_connect=min(resolved, 5.0),
        sock_read=resolved,
    )


async def get_shared_http_session(
    *,
    timeout_sec: float | None = None,
    headers: dict[str, str] | None = None,
) -> aiohttp.ClientSession:
    global _shared_http_session
    global _shared_http_session_lock

    if _shared_http_session_lock is None:
        _shared_http_session_lock = asyncio.Lock()

    if _shared_http_session is not None and not _shared_http_session.closed:
        return _shared_http_session

    async with _shared_http_session_lock:
        if _shared_http_session is None or _shared_http_session.closed:
            _shared_http_session = aiohttp.ClientSession(
                timeout=_build_timeout(timeout_sec),
                headers=headers or {"User-Agent": "discord-ai-bot/1.0"},
                json_serialize=aiohttp_dumps,
            )
            log.info("created shared aiohttp session for external HTTP")

    return _shared_http_session


async def close_shared_http_session() -> None:
    global _shared_http_session
    global _shared_http_session_lock
    if _shared_http_session is not None and not _shared_http_session.closed:
        await _shared_http_session.close()
        log.info("closed shared aiohttp session for external HTTP")
    _shared_http_session = None
    _shared_http_session_lock = None

    
