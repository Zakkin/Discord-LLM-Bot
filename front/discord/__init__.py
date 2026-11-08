"""front.discord: Discord 入出力・イベント・返信・コマンド操作サブプロジェクト。

注意: `front/` ディレクトリ自体を `PYTHONPATH` / `sys.path` に入れないでください
（`front/discord` が `discord.py` パッケージを隠してしまいます）。
常にリポジトリルートを `PYTHONPATH` に設定してください。
"""
from __future__ import annotations

from front.discord.adapter import DiscordAdapter

__all__ = ["DiscordAdapter"]
