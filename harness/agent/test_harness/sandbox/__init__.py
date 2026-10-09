"""AIエージェント・ハーネス 2-1. サンドボックス実行環境パッケージ

隔離された実行空間の提供（Docker, Firecracker, WebAssembly, 権限制限ローカル）と、
ホスト環境を保護するための権限制限を提供する。
"""
from __future__ import annotations

from pathlib import Path

from .docker_backend import DockerSandbox
from .interface import SandboxEnvironment
from .local_restricted import LocalRestrictedSandbox
from .types import DEFAULT_BLOCKED_COMMANDS, SandboxBackend, SandboxConfig, SandboxResult
from .wasm_backend import WasmSandbox


def create_sandbox(
    work_dir: Path | str,
    backend: SandboxBackend = SandboxBackend.LOCAL_RESTRICTED,
    timeout_sec: float = 30.0,
    allowed_commands: set[str] | None = None,
) -> SandboxEnvironment:
    """サンドボックス環境インスタンスを生成するファクトリ関数"""
    config = SandboxConfig(
        work_dir=Path(work_dir),
        timeout_sec=timeout_sec,
        allowed_commands=allowed_commands,
    )
    if backend == SandboxBackend.DOCKER:
        return DockerSandbox(config)
    elif backend == SandboxBackend.WASM:
        return WasmSandbox(config)
    else:
        return LocalRestrictedSandbox(config)


__all__ = [
    "SandboxBackend",
    "SandboxConfig",
    "SandboxResult",
    "DEFAULT_BLOCKED_COMMANDS",
    "SandboxEnvironment",
    "LocalRestrictedSandbox",
    "DockerSandbox",
    "WasmSandbox",
    "create_sandbox",
]
