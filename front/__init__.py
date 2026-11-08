"""front: プラットフォーム（Discord / ふたば / X）とエージェントの橋渡し層。

サブパッケージ:
    - `front.discord`: Discord 入出力（Bot 起動・イベント・返信・UI）
    - `front.hutaba`:  *.2chan.net（ふたば☆ちゃんねる）操作
    - `front.x`:       x.com 操作

直下モジュール:
    - `front.ir`:       中間表現（FrontEvent / AgentTask / AgentOutcome / FrontAction）
    - `front.adapter`:  PlatformAdapter プロトコル
    - `front.bridge`:   AgentBridge（中間表現 ⇄ harness/agent）
    - `front.registry`: アダプター / ドメインヘルパー登録

注意: `front/` ディレクトリ自体を `sys.path` に追加しないこと。
`front/discord` が `discord.py` ライブラリを隠してしまいます。PYTHONPATH は常にリポジトリルートにします。
"""
from __future__ import annotations

from front.ir import (
    Actor,
    AgentOutcome,
    AgentTask,
    Attachment,
    Constraints,
    FrontAction,
    FrontEvent,
    Location,
    Reference,
    Turn,
)

__all__ = [
    "Actor",
    "AgentOutcome",
    "AgentTask",
    "Attachment",
    "Constraints",
    "FrontAction",
    "FrontEvent",
    "Location",
    "Reference",
    "Turn",
]
