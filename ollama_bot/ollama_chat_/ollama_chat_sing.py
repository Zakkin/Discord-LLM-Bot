"""互換シム: このモジュールの実体は `front.discord.commands.sing` に移設されました。

旧パス `ollama_bot.ollama_chat_.ollama_chat_sing` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.commands.sing` を直接 import してください。
"""
import sys as _sys

import front.discord.commands.sing as _impl
from front.discord.commands.sing import *  # noqa: F401,F403
from front.discord.commands.sing import (  # noqa: F401
    _generate_singing_search_queries,
)

_sys.modules[__name__] = _impl
