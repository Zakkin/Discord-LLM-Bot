"""互換シム: このモジュールの実体は `front.x.parser` に移設されました。

旧パス `harness.agent.tool_registry.mcp.chrome.xcom` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.x.parser` を直接 import してください。
"""
import sys as _sys

import front.x.parser as _impl
from front.x.parser import *  # noqa: F401,F403

_sys.modules[__name__] = _impl
