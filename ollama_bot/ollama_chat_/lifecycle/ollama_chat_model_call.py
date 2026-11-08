"""互換シム: このモジュールの実体は `front.discord.lifecycle.ollama_chat_model_call` に移設されました。

旧パス `ollama_bot.ollama_chat_.lifecycle.ollama_chat_model_call` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.lifecycle.ollama_chat_model_call` を直接 import してください。
"""
import sys as _sys

import front.discord.lifecycle.ollama_chat_model_call as _impl
from front.discord.lifecycle.ollama_chat_model_call import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
