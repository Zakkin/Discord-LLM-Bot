"""互換シム: このモジュールの実体は `front.hutaba.thread_reader` に移設されました。

旧パス `harness.agent.tool_registry.web_research.img_thread` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.hutaba.thread_reader` を直接 import してください。
"""
import sys as _sys

import front.hutaba.thread_reader as _impl
from front.hutaba.thread_reader import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
