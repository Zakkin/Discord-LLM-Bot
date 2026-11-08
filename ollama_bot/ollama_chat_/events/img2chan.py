"""互換シム: このモジュールの実体は `front.hutaba.monitor.img2chan` に移設されました。

旧パス `ollama_bot.ollama_chat_.events.img2chan` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.hutaba.monitor.img2chan` を直接 import してください。
"""
import sys as _sys

import front.hutaba.monitor.img2chan as _impl
from front.hutaba.monitor.img2chan import *  # noqa: F401,F403
from front.hutaba.monitor.img2chan import (  # noqa: F401
    _Img2chanEventMixin,
    _get_img2chan_helper,
)

_sys.modules[__name__] = _impl
