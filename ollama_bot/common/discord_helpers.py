"""互換シム: このモジュールの実体は `front.discord.common.discord_helpers` に移設されました。

旧パス `ollama_bot.common.discord_helpers` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.common.discord_helpers` を直接 import してください。
"""
import sys as _sys

import front.discord.common.discord_helpers as _impl
from front.discord.common.discord_helpers import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
