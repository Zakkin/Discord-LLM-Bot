"""
[AI Agent Summary]
後方互換性のためのollama_reply_safetyアグリゲーター。実装は ollama_bot.common.reply_safety に移動しました。
Backward-compatibility aggregator for ollama_reply_safety. Implementation is moved to ollama_bot.common.reply_safety.
"""
from __future__ import annotations

import sys
from .reply_safety import *
from . import reply_safety as _reply_safety

sys.modules[__name__] = _reply_safety
