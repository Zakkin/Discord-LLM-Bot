"""Turn-level time budget management for Discord reply turns (§85).

Provides contextvar-backed deadline tracking so optional LLM stages
(pre-reply analysis, research decision, deviation/tone/supplemental retries,
and semantic send-gate regeneration) can be skipped when a turn exceeds its
time budget, ensuring primary reply generation and sending still complete.
"""

from __future__ import annotations

import contextvars
import math
import time

from ollama_bot.common.config_helpers import cfg_float

DEFAULT_TURN_BUDGET_SEC = 120.0

TURN_BUDGET: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "turn_budget_deadline",
    default=None,
)
_TURN_BUDGET_CONFIGURED: contextvars.ContextVar[float] = contextvars.ContextVar(
    "turn_budget_configured_sec",
    default=0.0,
)
_TURN_BUDGET_STARTED_AT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "turn_budget_started_at",
    default=None,
)
_TURN_BUDGET_SKIPPED: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "turn_budget_skipped_steps",
    default=(),
)


def resolve_turn_budget_sec() -> float:
    """Resolve REPLY_TURN_BUDGET_SEC from configuration (<= 0 disables)."""
    raw = cfg_float("REPLY_TURN_BUDGET_SEC", DEFAULT_TURN_BUDGET_SEC)
    if raw is None:
        return DEFAULT_TURN_BUDGET_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TURN_BUDGET_SEC
    if not math.isfinite(value):
        return DEFAULT_TURN_BUDGET_SEC
    return value


def start_turn_budget(budget_sec: float) -> None:
    """Start a turn budget bound to the current asyncio/contextvars context.

    Passing <= 0 disables the turn budget for the current turn.
    """
    now = time.monotonic()
    _TURN_BUDGET_STARTED_AT.set(now)
    _TURN_BUDGET_SKIPPED.set(())

    try:
        parsed = float(budget_sec)
    except (TypeError, ValueError):
        parsed = 0.0

    if not math.isfinite(parsed) or parsed <= 0.0:
        _TURN_BUDGET_CONFIGURED.set(0.0)
        TURN_BUDGET.set(None)
        return

    _TURN_BUDGET_CONFIGURED.set(parsed)
    TURN_BUDGET.set(now + parsed)


def clear_turn_budget() -> None:
    """Reset all turn budget context variables."""
    TURN_BUDGET.set(None)
    _TURN_BUDGET_CONFIGURED.set(0.0)
    _TURN_BUDGET_STARTED_AT.set(None)
    _TURN_BUDGET_SKIPPED.set(())


def turn_budget_remaining() -> float | None:
    """Return remaining seconds in the current turn budget, or None if disabled."""
    deadline = TURN_BUDGET.get()
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def turn_budget_exceeded() -> bool:
    """Return True iff a turn budget is active and its deadline has passed."""
    deadline = TURN_BUDGET.get()
    if deadline is None:
        return False
    return time.monotonic() >= deadline


def record_turn_budget_skip(step_name: str) -> None:
    """Record the name of an optional step skipped due to turn budget exhaustion."""
    name = str(step_name or "").strip()
    if not name:
        return
    current = _TURN_BUDGET_SKIPPED.get()
    if name not in current:
        _TURN_BUDGET_SKIPPED.set(current + (name,))


def get_turn_budget_skipped() -> list[str]:
    """Return skipped step names in insertion order."""
    return list(_TURN_BUDGET_SKIPPED.get())


def get_turn_budget_snapshot() -> tuple[float, float, bool, list[str]]:
    """Return (elapsed_sec, budget_sec, exceeded, skipped_steps) for logging."""
    now = time.monotonic()
    started_at = _TURN_BUDGET_STARTED_AT.get()
    elapsed_sec = max(0.0, now - started_at) if started_at is not None else 0.0
    deadline = TURN_BUDGET.get()
    configured = _TURN_BUDGET_CONFIGURED.get()
    if deadline is None:
        budget_sec = 0.0
        exceeded = False
    else:
        budget_sec = configured if configured > 0.0 else (
            max(0.0, deadline - started_at) if started_at is not None else 0.0
        )
        exceeded = now >= deadline
    return elapsed_sec, budget_sec, exceeded, list(_TURN_BUDGET_SKIPPED.get())
