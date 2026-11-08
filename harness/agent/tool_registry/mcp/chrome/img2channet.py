"""互換シム: このモジュールの実体は `front.hutaba.client.img2channet` に移設されました。

旧パス `harness.agent.tool_registry.mcp.chrome.img2channet` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.hutaba.client.img2channet` を直接 import してください。
"""
import sys as _sys

import front.hutaba.client.img2channet as _impl
from front.hutaba.client.img2channet import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
