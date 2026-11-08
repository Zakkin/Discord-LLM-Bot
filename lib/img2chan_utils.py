"""互換シム: このモジュールの実体は `front.hutaba.utils` に移設されました。

旧パス `lib.img2chan_utils` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.hutaba.utils` を直接 import してください。
"""
import sys as _sys

import front.hutaba.utils as _impl
from front.hutaba.utils import (  # noqa: F401
    _IMG_THREAD_URL_PATTERN,
    default_browser_headers,
    normalize_img_thread_url,
)

_sys.modules[__name__] = _impl
