"""互換シム: このモジュールの実体は `front.discord.commands.umigame` に移設されました。

旧パス `ollama_bot.ollama_chat_.ollama_chat_umigame` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.commands.umigame` を直接 import してください。
"""
import sys as _sys

import front.discord.commands.umigame as _impl
from front.discord.commands.umigame import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
