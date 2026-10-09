"""AIエージェント・ハーネス サンドボックス抽象インターフェース (interface.py)

2-1. サンドボックス実行環境の共通インターフェースを定義。
ホスト環境から隔離された実行空間の提供と、共通の操作規約を規定する。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from .types import SandboxConfig, SandboxResult


class SandboxEnvironment(ABC):
    """隔離された実行空間を提供するサンドボックスの基底クラス"""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    @abstractmethod
    async def initialize(self) -> None:
        """環境の初期化・準備（ディレクトリ作成、コンテナ起動等）"""
        pass

    @abstractmethod
    async def execute_command(
        self,
        command: str | Sequence[str],
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """隔離空間内でコマンドを実行する。

        Args:
            command: 実行するコマンド文字列または引数リスト
            timeout_sec: タイムアウト秒（省略時はconfigの設定値）

        Returns:
            SandboxResult: 実行結果
        """
        pass

    @abstractmethod
    async def read_file(self, relative_path: str) -> str:
        """隔離空間内のファイルを安全に読み出す。"""
        pass

    @abstractmethod
    async def write_file(self, relative_path: str, content: str) -> bool:
        """隔離空間内にファイルを安全に書き込む。"""
        pass

    @abstractmethod
    async def clean_up(self) -> None:
        """環境の破棄・クリーンアップ（一時ファイル消去、コンテナ停止等）"""
        pass
