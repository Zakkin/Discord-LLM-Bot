"""互換シム: 実体は `front.hutaba.client.img2chan` に移設されました。

通常の import では新モジュールへ透過的に転送します（mock.patch も新モジュールに作用）。
`importlib.util.spec_from_file_location` でこのファイルが直接読み込まれた場合は、
従来どおり独立したモジュールとして実装本体を実行します。
"""
import sys as _sys
from pathlib import Path as _Path

if __name__ == "harness.agent.tool_registry.mcp.chrome.img2chan":
    import front.hutaba.client.img2chan as _impl

    _sys.modules[__name__] = _impl
else:
    _IMPL_PATH = _Path(__file__).resolve().parents[5] / "front/hutaba/client/img2chan.py"
    __file__ = str(_IMPL_PATH)
    exec(compile(_IMPL_PATH.read_text(encoding="utf-8"), str(_IMPL_PATH), "exec"), globals(), globals())
