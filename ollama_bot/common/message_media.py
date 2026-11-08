"""互換シム: このモジュールの実体は `front.discord.common.message_media` に移設されました。

旧パス `ollama_bot.common.message_media` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.common.message_media` を直接 import してください。
"""
import sys as _sys

import front.discord.common.message_media as _impl
from front.discord.common.message_media import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
