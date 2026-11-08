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
    cfg_command_ignored_user_ids,
    cfg_emotion_bio_enabled,
    cfg_emotion_bio_multi_config_enabled,
    cfg_emotion_bio_multi_config_merge_enabled,
    cfg_float,
    cfg_int,
    cfg_int_set,
    cfg_managed_channel_ids,
    cfg_persona_namespace,
    cfg_primary_channel_id,
    cfg_str,
    config,
    get_bot_config_prefix,
    is_user_command_blocked,
    model_supports_thinking,
    resolve_bot_guild_id,
    resolve_bot_token,
    resolve_emotion_bio_label,
)

__all__ = [
    "T",
    "cfg",
    "cfg_bool",
    "cfg_command_ignored_user_ids",
    "cfg_emotion_bio_enabled",
    "cfg_emotion_bio_multi_config_enabled",
    "cfg_emotion_bio_multi_config_merge_enabled",
    "cfg_float",
    "cfg_int",
    "cfg_int_set",
    "cfg_managed_channel_ids",
    "cfg_persona_namespace",
    "cfg_primary_channel_id",
    "cfg_str",
    "config",
    "get_bot_config_prefix",
    "is_user_command_blocked",
    "model_supports_thinking",
    "resolve_bot_guild_id",
    "resolve_bot_token",
    "resolve_emotion_bio_label",
]
