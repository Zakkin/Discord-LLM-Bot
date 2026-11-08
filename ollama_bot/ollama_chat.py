"""互換シム: このモジュールの実体は `front.discord.cog` に移設されました。

旧パス `ollama_bot.ollama_chat` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.cog` を直接 import してください。
"""
import sys as _sys

import front.discord.cog as _impl
from front.discord.cog import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
