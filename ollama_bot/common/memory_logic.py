"""
[AI Agent Summary]
後方互換性のためのmemory_logicアグリゲーター。実装は ollama_bot.common.memory.logic に移動しました。
Backward-compatibility aggregator for memory_logic. Implementation is moved to ollama_bot.common.memory.logic.
"""
from __future__ import annotations

import sys
from .memory.logic import *
from .memory.logic import (
    _persona_from_cfg,
    _cfg_bool,
    _embed_model_from_cfg,
    _is_fallback_reply,
    _habit_profile_model_name,
)
from .memory import logic as _logic

sys.modules[__name__] = _logic
