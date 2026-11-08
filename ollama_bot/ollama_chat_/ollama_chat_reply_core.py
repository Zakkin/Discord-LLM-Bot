"""互換シム: このモジュールの実体は `front.discord.reply.reply_core` に移設されました。

旧パス `ollama_bot.ollama_chat_.ollama_chat_reply_core` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.reply.reply_core` を直接 import してください。
"""
import sys as _sys

import front.discord.reply.reply_core as _impl
from front.discord.reply.reply_core import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
