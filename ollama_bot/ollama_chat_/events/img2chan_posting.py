"""互換シム: このモジュールの実体は `front.hutaba.monitor.img2chan_posting` に移設されました。

旧パス `ollama_bot.ollama_chat_.events.img2chan_posting` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.hutaba.monitor.img2chan_posting` を直接 import してください。
"""
import sys as _sys

import front.hutaba.monitor.img2chan_posting as _impl
from front.hutaba.monitor.img2chan_posting import *  # noqa: F401,F403
from front.hutaba.monitor.img2chan_posting import (  # noqa: F401
    _Img2chanPostingMixin,
)

_sys.modules[__name__] = _impl
