"""互換シム: このモジュールの実体は `front.discord.utils` に移設されました。

旧パス `lib.discord_utils` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.utils` を直接 import してください。
"""
import sys as _sys

import front.discord.utils as _impl
from front.discord.utils import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
