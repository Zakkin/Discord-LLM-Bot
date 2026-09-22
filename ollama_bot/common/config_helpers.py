# ollama_bot/common/config_helpers.py
"""
[AI Agent Summary]
後方互換性のためのconfig_helpersアグリゲーター。実装は lib.config_utils に移動しました。
Backward-compatibility aggregator for config_helpers. Implementation is moved to lib.config_utils.
"""
from __future__ import annotations

from lib.config_utils import (
    T,
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_int_set,
    cfg_managed_channel_ids,
    cfg_primary_channel_id,
    cfg_str,
    config,
    model_supports_thinking,
)

__all__ = [
    "T",
    "cfg",
    "cfg_bool",
    "cfg_float",
    "cfg_int",
    "cfg_int_set",
    "cfg_managed_channel_ids",
    "cfg_primary_channel_id",
    "cfg_str",
    "config",
    "model_supports_thinking",
]
