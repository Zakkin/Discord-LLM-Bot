"""AIエージェント・ハーネス オーケストレーター 定期実行ループモジュール (orchestrator/loops.py)

定期実行ループ（agent_tick, reflection, topic）と割り込み可能な背景LLMタスク管理を担当するMixin。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Coroutine, TYPE_CHECKING

from lib.config_utils import (
    cfg,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
)
from .reflection import safe_close_coroutine

log = logging.getLogger("harness.agent.orchestrator.loops")

if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _AgentLoopsBase = OllamaChatProtocol
else:
    _AgentLoopsBase = object


class AgentLoopsMixin(_AgentLoopsBase):
    """定期実行ループ（agent_tick, reflection, topic）とタスク管理Mixin"""

    def _is_user_reply_busy(self) -> bool:
        """ユーザー返信処理または感情更新処理がアクティブ（LLM推論中・キュー待機中）か判定する。"""
        reply_queue = getattr(self, "_reply_queue", None)
        if reply_queue is not None:
            try:
                if not reply_queue.empty():
                    return True
            except Exception:
                pass
            if getattr(reply_queue, "_unfinished_tasks", 0) > 0:
                return True

        emotion_tasks = getattr(self, "_emotion_update_tasks", None)
        if emotion_tasks is not None:
            if isinstance(emotion_tasks, (list, tuple, set)):
                if any(getattr(t, "done", None) is not None and not t.done() for t in emotion_tasks):
                    return True
            elif isinstance(emotion_tasks, dict):
                if any(getattr(t, "done", None) is not None and not t.done() for t in emotion_tasks.values()):
                    return True
            elif hasattr(emotion_tasks, "done") and not emotion_tasks.done():
                return True

        active_cids = getattr(self, "_active_normal_reply_cids", None)
        if active_cids and len(active_cids) > 0:
            return True

        return False

    async def _run_cancellable_background_task(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._is_user_reply_busy():
            safe_close_coroutine(coro)
            log.info("background LLM task skipped: user reply is in progress")
            return

        current_task = getattr(self, "_current_background_llm_task", None)
        if current_task is not None and not current_task.done():
            safe_close_coroutine(coro)
            log.info("background task skipped: another background LLM task is already running")
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
            if self._is_user_reply_busy():
                log.info("reflection loop: user reply is busy, postponing reflection")
                continue
            try:
                persona_namespace = cfg_persona_namespace(custom_cfg=cfg)
                await self.memory_store.decay_and_purge_memories(
                    persona=persona_namespace,
                    decay_ratio=cfg_float("MEMORY_DECAY_RATIO", 0.95),
                    purge_threshold=cfg_float("MEMORY_PURGE_SCORE", 0.20),
                    purge_days=cfg_float("MEMORY_PURGE_DAYS", 5.0),
                )
                if self._is_user_reply_busy():
                    log.info("reflection loop: user reply in progress after memory decay, skipping reflection LLM task")
                    continue
                await self._run_cancellable_background_task(self._run_reflection_once())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("reflection loop failed: %s", e)

    async def _agent_tick_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(max(cfg_int("AGENT_TICK_INTERVAL_SEC", 15 * 60), 60))
            if self._is_user_reply_busy():
                log.info("agent tick loop: user reply is busy, postponing agent tick")
                continue
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

                    if not getattr(self, "is_active", True):
                        continue

                    if self._is_user_reply_busy():
                        log.info("agent tick: user reply in progress, skipping background LLM action")
                        continue

                    check_cd = getattr(self, "is_inner_monologue_in_cooldown", None)
                    if callable(check_cd):
                        is_monologue_cd = bool(check_cd())
                    else:
                        last_thought = float(getattr(self.emotion_state, "last_thought_ts", 0.0) or 0.0)
                        is_monologue_cd = (now - last_thought) < cfg_float("AGENT_INNER_MONOLOGUE_COOLDOWN_SEC", 2 * 3600.0)

                    curiosity_thresh = cfg_float("AGENT_CURIOSITY_TRIGGER", 0.68)
                    boredom_thresh = cfg_float("AGENT_BOREDOM_TRIGGER", 0.82)

                    if curiosity >= curiosity_thresh and not is_monologue_cd:
                        log.info("agent tick: curiosity threshold met, triggering inner monologue")
                        await self._run_cancellable_background_task(self._execute_inner_monologue_and_learn())
                    elif boredom >= boredom_thresh:
                        log.info("agent tick: boredom threshold met, evaluating proactive action eligibility")
                        await self._run_cancellable_background_task(self._proactive_action())
                    elif curiosity >= curiosity_thresh and is_monologue_cd:
                        log.info("[monologue] Inner monologue skipped: reason=cooldown (tick loop)")
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
            if not getattr(self, "is_active", True):
                continue
            if self._is_user_reply_busy():
                log.info("ollama topic loop: user reply is busy, skipping topic post")
                continue
            try:
                await self._run_cancellable_background_task(self._proactive_action())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("ollama topic loop failed: %s", e)


_AgentLoopsMixin = AgentLoopsMixin
OrchestratorLoopsMixin = AgentLoopsMixin

