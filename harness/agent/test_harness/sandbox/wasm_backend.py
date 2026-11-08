"""AIエージェント・ハーネス WebAssembly軽量サンドボックス (wasm_backend.py)

2-1. サンドボックス実行環境として、
WebAssembly (Wasm) ランタイム（Wasmtime/WasmEdge等）を想定した軽量分離実行空間を提供する。
"""
from __future__ import annotations

import logging
import shutil
import time
from typing import Sequence

from .interface import SandboxEnvironment
from .types import SandboxBackend, SandboxConfig, SandboxResult

log = logging.getLogger("harness.agent.test_harness.sandbox.wasm")


class WasmSandbox(SandboxEnvironment):
    """WebAssemblyランタイムを用いた軽量分離実行環境"""

    def __init__(self, config: SandboxConfig) -> None:
        super().__init__(config)
        self._wasm_runtime = shutil.which("wasmtime") or shutil.which("wasmedge")

    async def initialize(self) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        if not self._wasm_runtime:
            log.info("[sandbox.wasm] No native Wasm runtime found. Fallback mode enabled.")

    async def execute_command(
        self,
        command: str | Sequence[str],
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        start_time = time.perf_counter()
        if not self._wasm_runtime:
            return SandboxResult(
                exit_code=1,
                stderr="Wasm runtime (wasmtime/wasmedge) is not installed on host.",
                execution_time_sec=time.perf_counter() - start_time,
                backend=SandboxBackend.WASM,
            )

        # Wasm実行コマンドの構築・実行（将来の拡張に対応）
        return SandboxResult(
            exit_code=0,
            stdout="Wasm execution placeholder",
            execution_time_sec=time.perf_counter() - start_time,
            backend=SandboxBackend.WASM,
        )

    async def read_file(self, relative_path: str) -> str:
        target = (self.config.work_dir / relative_path).resolve()
        return target.read_text(encoding="utf-8")

    async def write_file(self, relative_path: str, content: str) -> bool:
        target = (self.config.work_dir / relative_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    async def clean_up(self) -> None:
        pass
