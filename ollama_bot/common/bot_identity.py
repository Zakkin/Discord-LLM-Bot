"""互換シム: このモジュールの実体は `front.discord.common.bot_identity` に移設されました。

旧パス `ollama_bot.common.bot_identity` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.common.bot_identity` を直接 import してください。
"""
import sys as _sys

import front.discord.common.bot_identity as _impl
from front.discord.common.bot_identity import *  # noqa: F401,F403
from front.discord.common.bot_identity import (  # noqa: F401
    _extract_character_names_from_config,
)

_sys.modules[__name__] = _impl
