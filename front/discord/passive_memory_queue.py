"""Discord 受動メモリ抽出のアイドルゲート（保留キュー・純粋判定・ドレインループ）（§168）。

返信見送り時の受動的長期記憶抽出・要約更新を会話中の即時実行から切り離し、
保留キューへ積んで会話の切れ目（または滞留上限超過時）に1件ずつ安全に実行する。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Awaitable, Callable

_log = logging.getLogger("front.discord.passive_memory_queue")

DEFAULT_IDLE_GATE_ENABLED = True
DEFAULT_IDLE_SEC = 120.0
DEFAULT_MAX_WAIT_SEC = 1800.0
DEFAULT_DRAIN_INTERVAL_SEC = 20.0
DEFAULT_QUEUE_MAX = 50


@dataclass
class PassiveMemoryJob:
    """保留キューに積む受動メモリ抽出ジョブ。message は不透明に保持する。"""

    cid: int
    author_id: int
    message: Any
    user_text: str
    should_update_summary: bool
    enqueued_ts: float


class PassiveMemoryQueue:
    """(cid, author_id) 単位で統合し、最古から取り出す有界保留キュー。"""

    def __init__(self, max_size: int = DEFAULT_QUEUE_MAX) -> None:
        self._max_size = max(1, int(max_size))
        self._entries: dict[tuple[int, int], tuple[int, PassiveMemoryJob]] = {}
        self._seq = 0

    def __len__(self) -> int:
        return len(self._entries)

    def _oldest_key(self) -> tuple[int, int] | None:
        if not self._entries:
            return None
        return min(
            self._entries,
            key=lambda k: (self._entries[k][1].enqueued_ts, self._entries[k][0]),
        )

    def enqueue(self, job: PassiveMemoryJob) -> tuple[int, bool, bool]:
        """ジョブを積み、(現在件数, 統合したか, 最古を破棄したか) を返す。"""
        key = (job.cid, job.author_id)
        if key in self._entries:
            seq, existing = self._entries[key]
            merged = PassiveMemoryJob(
                cid=existing.cid,
                author_id=existing.author_id,
                message=job.message,
                user_text=job.user_text,
                should_update_summary=bool(
                    existing.should_update_summary or job.should_update_summary
                ),
                enqueued_ts=existing.enqueued_ts,
            )
            self._entries[key] = (seq, merged)
            return (len(self._entries), True, False)

        dropped = False
        if len(self._entries) >= self._max_size:
            oldest_key = self._oldest_key()
            if oldest_key is not None:
                _, dropped_job = self._entries.pop(oldest_key)
                dropped = True
                _log.warning(
                    "[PASSIVE_MEMORY] queue overflow: dropped oldest channel=%s",
                    dropped_job.cid,
                )

        self._seq += 1
        self._entries[key] = (self._seq, job)
        return (len(self._entries), False, dropped)

    def oldest(self) -> PassiveMemoryJob | None:
        oldest_key = self._oldest_key()
        if oldest_key is None:
            return None
        return self._entries[oldest_key][1]

    def pop_oldest(self) -> PassiveMemoryJob | None:
        oldest_key = self._oldest_key()
        if oldest_key is None:
            return None
        _, job = self._entries.pop(oldest_key)
        return job


def decide_drain(
    *,
    enabled: bool,
    pending: int,
    reply_busy: bool,
    last_activity_ts: float,
    oldest_enqueued_ts: float | None,
    now: float,
    idle_sec: float,
    max_wait_sec: float,
) -> tuple[bool, str]:
    """保留ジョブを今1件実行してよいか純粋判定する。"""
    if not enabled:
        return (False, "disabled")
    if pending <= 0:
        return (False, "empty")
    if reply_busy:
        return (False, "reply_busy")
    if (now - last_activity_ts) >= idle_sec:
        return (True, "idle")
    if oldest_enqueued_ts is not None and (now - oldest_enqueued_ts) >= max_wait_sec:
        return (True, "max_wait")
    return (False, "waiting_idle")


async def run_drain_once(
    queue: PassiveMemoryQueue,
    *,
    run_job: Callable[[PassiveMemoryJob], Awaitable[Any]],
    reply_busy: bool,
    last_activity_ts: float,
    now: float,
    idle_sec: float,
    max_wait_sec: float,
    enabled: bool,
    log: Any,
) -> str:
    """判定結果が実行可なら最古ジョブを1件だけ消費・実行し、理由文字列を返す。"""
    oldest_job = queue.oldest()
    should_drain, reason = decide_drain(
        enabled=enabled,
        pending=len(queue),
        reply_busy=reply_busy,
        last_activity_ts=last_activity_ts,
        oldest_enqueued_ts=(oldest_job.enqueued_ts if oldest_job is not None else None),
        now=now,
        idle_sec=idle_sec,
        max_wait_sec=max_wait_sec,
    )
    if not should_drain:
        return reason

    job = queue.pop_oldest()
    if job is None:
        return "empty"

    logger = log if log is not None else _log
    waited_sec = max(0.0, now - job.enqueued_ts)
    logger.info(
        "[PASSIVE_MEMORY] drained: channel=%s waited_sec=%.0f reason=%s pending=%d",
        job.cid,
        waited_sec,
        reason,
        len(queue),
    )
    try:
        await run_job(job)
    except Exception as exc:
        logger.warning(
            "[PASSIVE_MEMORY] drain failed: channel=%s error=%r",
            job.cid,
            exc,
        )
    return reason


async def passive_memory_drain_loop(
    queue: PassiveMemoryQueue,
    *,
    run_job: Callable[[PassiveMemoryJob], Awaitable[Any]],
    is_reply_busy: Callable[[], bool],
    last_activity_ts_fn: Callable[[], float],
    settings_fn: Callable[[], dict[str, Any]],
    log: Any,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """周期的に保留キューを点検し、条件を満たせば1件ずつ実行するループ。"""
    logger = log if log is not None else _log
    while True:
        interval_sec = DEFAULT_DRAIN_INTERVAL_SEC
        try:
            settings = settings_fn() or {}
            enabled = bool(settings.get("enabled", DEFAULT_IDLE_GATE_ENABLED))
            idle_sec = float(settings.get("idle_sec", DEFAULT_IDLE_SEC))
            max_wait_sec = float(settings.get("max_wait_sec", DEFAULT_MAX_WAIT_SEC))
            raw_interval = float(settings.get("interval_sec", DEFAULT_DRAIN_INTERVAL_SEC))
            if raw_interval > 0:
                interval_sec = raw_interval
            await run_drain_once(
                queue,
                run_job=run_job,
                reply_busy=bool(is_reply_busy()),
                last_activity_ts=float(last_activity_ts_fn()),
                now=time.time(),
                idle_sec=idle_sec,
                max_wait_sec=max_wait_sec,
                enabled=enabled,
                log=logger,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[PASSIVE_MEMORY] drain loop error: %r", exc)
        await sleep(interval_sec)

