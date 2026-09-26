from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .client import _get_ollama_session
from .resolve import resolve_cfg, resolve_ollama_fn

log = logging.getLogger("ollama_bot.common.ollama_helpers.embeddings")

_cfg = resolve_cfg


async def call_ollama_embeddings(
    text: str,
    *,
    model: str,
    timeout_sec: float | None = None,
    options: dict[str, Any] | None = None,
) -> list[float]:
    if not text.strip() or not model.strip():
        return []

    timeout = aiohttp.ClientTimeout(total=timeout_sec or float(_cfg("OLLAMA_TIMEOUT_SEC", 30.0) or 30.0))
    session_fn = resolve_ollama_fn("_get_ollama_session", _get_ollama_session)
    session = await session_fn()

    # 1. 最新の /api/embed を試行
    embed_payload: dict[str, Any] = {
        "model": model.strip(),
        "input": text.strip(),
    }
    if options:
        embed_payload["options"] = options

    try:
        async with session.post("/api/embed", json=embed_payload, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                embeddings = data.get("embeddings") or []
                if embeddings and isinstance(embeddings[0], list):
                    return [float(x) for x in embeddings[0]]
    except Exception as e:
        log.debug("ollama /api/embed failed (will try fallback): model=%s err=%r", model, e)

    # 2. 旧仕様 /api/embeddings へのフォールバック
    payload: dict[str, Any] = {
        "model": model.strip(),
        "prompt": text.strip(),
    }
    if options:
        payload["options"] = options

    try:
        async with session.post("/api/embeddings", json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.json()
            raw_vec = data.get("embedding") or []
            return [float(x) for x in raw_vec]
    except Exception as e:
        log.warning("ollama embeddings failed model=%s err=%r", model, e)
        return []


async def call_ollama_batch_embeddings(
    texts: list[str],
    *,
    model: str,
    timeout_sec: float | None = None,
    options: dict[str, Any] | None = None,
) -> list[list[float]]:
    valid_texts = [t.strip() for t in texts if t and t.strip()]
    if not valid_texts or not model.strip():
        return []

    timeout = aiohttp.ClientTimeout(total=timeout_sec or float(_cfg("OLLAMA_TIMEOUT_SEC", 30.0) or 30.0))
    session_fn = resolve_ollama_fn("_get_ollama_session", _get_ollama_session)
    session = await session_fn()

    # 1. /api/embed で一括取得
    embed_payload: dict[str, Any] = {
        "model": model.strip(),
        "input": valid_texts,
    }
    if options:
        embed_payload["options"] = options

    try:
        async with session.post("/api/embed", json=embed_payload, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                embeddings = data.get("embeddings") or []
                if isinstance(embeddings, list) and len(embeddings) > 0:
                    return [[float(x) for x in vec] for vec in embeddings if isinstance(vec, list)]
    except Exception as e:
        log.debug("ollama /api/embed batch failed (will try fallback): model=%s err=%r", model, e)

    # 2. フォールバック: 個別 /api/embeddings を並行取得
    try:
        call_embeddings_fn = resolve_ollama_fn("call_ollama_embeddings", call_ollama_embeddings)
        tasks = [
            call_embeddings_fn(t, model=model, timeout_sec=timeout_sec, options=options)
            for t in valid_texts
        ]
        results = await asyncio.gather(*tasks)
        return list(results)
    except Exception as e:
        log.warning("ollama batch embeddings fallback failed model=%s err=%r", model, e)
        return []
