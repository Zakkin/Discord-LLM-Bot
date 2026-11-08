"""互換シム: 実体は `front/hutaba/client/img2channet_parts/00_constants.py` に移設されました。"""
from pathlib import Path as _Path

_PART = _Path(__file__).resolve().parents[6] / "front/hutaba/client/img2channet_parts/00_constants.py"
exec(compile(_PART.read_text(encoding="utf-8"), str(_PART), "exec"), globals(), globals())
