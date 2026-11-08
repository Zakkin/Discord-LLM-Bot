"""互換シム: このモジュールの実体は `front.discord.admin` に移設されました。

旧パス `ollama_bot.ollama_chat_.ollama_chat_admin` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.admin` を直接 import してください。
"""
import sys as _sys

import front.discord.admin as _impl
from front.discord.admin import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
