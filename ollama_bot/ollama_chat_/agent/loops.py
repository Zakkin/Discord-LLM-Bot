"""
[AI Agent Summary]
定期実行ループ（agent_tick, reflection, topic）と割り込み可能な背景LLMタスク管理を担当するMixin。
This module handles periodic background loops (agent_tick, reflection, topic) and cancellable background LLM task management.
"""
from __future__ import annotations

import asyncio
import random
import time

from typing import Any, Coroutine, TYPE_CHECKING

from ...common.config_helpers import (
    cfg,
    cfg_float,
    cfg_int,
)
from ..ollama_chat_helpers import log

if TYPE_CHECKING:
    from ..ollama_chat_types import OllamaChatProtocol
    _AgentLoopsBase = OllamaChatProtocol
else:
    _AgentLoopsBase = object


class _AgentLoopsMixin(_AgentLoopsBase):
    async def _run_cancellable_background_task(self, coro: Coroutine[Any, Any, None]) -> None:
        current_task = getattr(self, "_current_background_llm_task", None)
        if current_task is not None and not current_task.done():
            return  # Skip if another background LLM task is already running

        task = asyncio.create_task(coro)
        self._current_background_llm_task = task
        try:
            await task
        except asyncio.CancelledError:
            log.info("background LLM task was cancelled (preempted)")
        finally:
            if getattr(self, "_current_background_llm_task", None) is task:
                self._current_background_llm_task = None

    async def _reflection_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(max(int(cfg("REFLECTION_TICK_INTERVAL_SEC", 1800) or 1800), 300))
            try:
                persona_namespace = str(cfg("MEMORY_PERSONA_NAMESPACE", "default") or "default")
                await self.memory_store.decay_and_purge_memories(
                    persona=persona_namespace,
                    decay_ratio=cfg_float("MEMORY_DECAY_RATIO", 0.95),
                    purge_threshold=cfg_float("MEMORY_PURGE_SCORE", 0.20),
                    purge_days=cfg_float("MEMORY_PURGE_DAYS", 5.0),
                )
                await self._run_cancellable_background_task(self._run_reflection_once())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reflection loop failed: %s", e)

    async def _agent_tick_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(max(cfg_int("AGENT_TICK_INTERVAL_SEC", 15 * 60), 60))
            try:
                async with self._agent_action_lock:
                    now = time.time()
                    async with self._emotion_lock:
                        last_tick_ts = float(self.emotion_state.last_agent_tick_ts or 0.0)
                    elapsed = (now - last_tick_ts) if last_tick_ts > 0 else float(cfg_int("AGENT_TICK_INTERVAL_SEC", 15 * 60))
                    await self._update_agent_needs(elapsed_sec=elapsed)

                    async with self._emotion_lock:
                        boredom = float(self.emotion_state.boredom or 0.0)
                        curiosity = float(self.emotion_state.curiosity or 0.0)
                        loneliness = float(self.emotion_state.loneliness or 0.0)
                        mood = str(self.emotion_state.mood or "neutral")

                    log.info("agent tick: boredom=%.2f loneliness=%.2f curiosity=%.2f mood=%s",
                             boredom, loneliness, curiosity, mood)

                    if curiosity >= cfg_float("AGENT_CURIOSITY_TRIGGER", 0.68):
                        log.info("agent tick: curiosity threshold met, triggering inner monologue")
                        await self._run_cancellable_background_task(self._execute_inner_monologue_and_learn())
                    elif boredom >= cfg_float("AGENT_BOREDOM_TRIGGER", 0.82):
                        log.info("agent tick: boredom threshold met, triggering proactive action")
                        await self._run_cancellable_background_task(self._proactive_action())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("agent tick loop failed: %s", e)

    async def _topic_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            lo = int(cfg("OLLAMA_TOPIC_INTERVAL_MIN_SEC", 6300) or 6300)
            hi = max(int(cfg("OLLAMA_TOPIC_INTERVAL_MAX_SEC", 8100) or 8100), lo)
            await asyncio.sleep(random.randint(lo, hi))
            try:
                await self._run_cancellable_background_task(self._proactive_action())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("ollama topic loop failed: %s", e)
