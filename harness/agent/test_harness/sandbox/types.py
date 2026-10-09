"""AIエージェント・ハーネス サンドボックス型定義 (types.py)

2-1. サンドボックス実行環境において、
隔離空間の設定、実行結果、セキュリティ制約、実行バックエンドの型を定義する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SandboxBackend(str, Enum):
    """サンドボックスの実行バックエンド種別"""
    LOCAL_RESTRICTED = "local_restricted"  # 権限制限・パス隔離されたローカル環境
    DOCKER = "docker"                      # Dockerコンテナ隔離環境
    WASM = "wasm"                          # WebAssembly軽量サンドボックス
    FIRECRACKER = "firecracker"            # Firecracker MicroVM


# ホスト環境を破壊・脅かす危険コマンドの禁止リスト
DEFAULT_BLOCKED_COMMANDS: frozenset[str] = frozenset({
    "rm -rf /",
    ":(){ :|:& };:",  # fork bomb
    "mkfs",
    "dd",
    "sudo",
    "su",
    "shutdown",
    "reboot",
    "chmod -R 777 /",
    "chown",
    "mount",
    "umount",
    "iptables",
})


@dataclass
class SandboxConfig:
    """サンドボックス環境の制約・セキュリティ設定"""
    work_dir: Path
    timeout_sec: float = 30.0
    max_output_bytes: int = 100_000
    allowed_commands: set[str] | None = None  # Noneの場合はホワイトリスト制限なし（blocked_commandsを適用）
    blocked_commands: set[str] = field(default_factory=lambda: set(DEFAULT_BLOCKED_COMMANDS))
    allowed_paths: list[Path] = field(default_factory=list)
    read_only_root: bool = True
    network_disabled: bool = True
    memory_limit_mb: int = 512
    cpu_limit_cores: float = 1.0


@dataclass
class SandboxResult:
    """サンドボックス内でのコマンド・スクリプト実行結果"""
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    execution_time_sec: float = 0.0
    timed_out: bool = False
    security_violation: str | None = None
    backend: SandboxBackend = SandboxBackend.LOCAL_RESTRICTED

    @property
    def success(self) -> bool:
        """実行が成功したか（終了コード0かつセキュリティ違反なし）"""
        return self.exit_code == 0 and not self.timed_out and self.security_violation is None
