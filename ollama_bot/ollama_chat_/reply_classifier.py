"""互換シム: このモジュールの実体は `front.discord.reply.classifier` に移設されました。

旧パス `ollama_bot.ollama_chat_.reply_classifier` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.reply.classifier` を直接 import してください。
"""
import sys as _sys

import front.discord.reply.classifier as _impl
from front.discord.reply.classifier import *  # noqa: F401,F403
from front.discord.reply.classifier import (  # noqa: F401
    _calculate_elapsed_sec,
    _calculate_gap,
    _calculate_human_gap,
    _extract_keywords,
    _is_ambient_commentary,
    _is_conversational_dialogue,
    _is_mascot_fear_short_reaction,
    _is_topic_interrupted,
    _starts_with_demonstrative_and_lacks_context,
)

_sys.modules[__name__] = _impl
