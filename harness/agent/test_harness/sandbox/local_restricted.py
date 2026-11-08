"""AIエージェント・ハーネス 権限制限ローカルサンドボックス (local_restricted.py)

2-1. サンドボックス実行環境として、
ホスト環境を保護するための厳格な権限制限、パストラバーサル遮断、
危険コマンドフィルタリングを備えたローカル隔離空間を提供する。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Sequence

from .interface import SandboxEnvironment
from .types import SandboxBackend, SandboxConfig, SandboxResult

log = logging.getLogger("harness.agent.test_harness.sandbox.local")


class LocalRestrictedSandbox(SandboxEnvironment):
    """ホスト保護・権限制限付きローカルサンドボックス実装"""

    def __init__(self, config: SandboxConfig) -> None:
        super().__init__(config)
        self.work_dir = config.work_dir.resolve()

    async def initialize(self) -> None:
        """作業ディレクトリを準備"""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        log.info("[sandbox.local] Initialized sandbox at %s", self.work_dir)

    async def execute_command(
        self,
        command: str | Sequence[str],
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """コマンドを実行（危険コマンド検査・タイムアウト・権限制限付き）"""
        start_time = time.perf_counter()
        effective_timeout = timeout_sec if timeout_sec is not None else self.config.timeout_sec

        if isinstance(command, str):
            cmd_str = command
            cmd_args = shlex.split(command)
        else:
            cmd_args = list(command)
            cmd_str = " ".join(cmd_args)

        if not cmd_args:
            return SandboxResult(
                exit_code=1,
                stderr="Empty command",
                execution_time_sec=0.0,
                backend=SandboxBackend.LOCAL_RESTRICTED,
            )

        # 1. 危険コマンド・セキュリティチェック
        base_cmd = os.path.basename(cmd_args[0])
        for blocked in self.config.blocked_commands:
            if blocked in cmd_str:
                violation = f"Security violation: Blocked command pattern '{blocked}' detected."
                log.warning("[sandbox.local] %s", violation)
                return SandboxResult(
                    exit_code=126,
                    stderr=violation,
                    execution_time_sec=time.perf_counter() - start_time,
                    security_violation=violation,
                    backend=SandboxBackend.LOCAL_RESTRICTED,
                )

        if self.config.allowed_commands is not None:
            if base_cmd not in self.config.allowed_commands and cmd_args[0] not in self.config.allowed_commands:
                violation = f"Security violation: Command '{cmd_args[0]}' is not in allowed list."
                log.warning("[sandbox.local] %s", violation)
                return SandboxResult(
                    exit_code=126,
                    stderr=violation,
                    execution_time_sec=time.perf_counter() - start_time,
                    security_violation=violation,
                    backend=SandboxBackend.LOCAL_RESTRICTED,
                )

        # 2. サブプロセス実行
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd_args[0],
                *cmd_args[1:],
                cwd=str(self.work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
            elapsed = time.perf_counter() - start_time

            # 出力サイズ制限
            stdout_str = stdout_bytes[: self.config.max_output_bytes].decode("utf-8", errors="replace")
            stderr_str = stderr_bytes[: self.config.max_output_bytes].decode("utf-8", errors="replace")

            return SandboxResult(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_str,
                stderr=stderr_str,
                execution_time_sec=elapsed,
                backend=SandboxBackend.LOCAL_RESTRICTED,
            )

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start_time
            log.warning("[sandbox.local] Command '%s' timed out after %.1fs", cmd_str, effective_timeout)
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return SandboxResult(
                exit_code=-1,
                stderr=f"Command timed out after {effective_timeout:.1f}s",
                execution_time_sec=elapsed,
                timed_out=True,
                backend=SandboxBackend.LOCAL_RESTRICTED,
            )
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            log.exception("[sandbox.local] Execution failed: %s", e)
            return SandboxResult(
                exit_code=-1,
                stderr=f"Execution error: {type(e).__name__}: {e}",
                execution_time_sec=elapsed,
                backend=SandboxBackend.LOCAL_RESTRICTED,
            )

    async def read_file(self, relative_path: str) -> str:
        """安全なファイル読み出し（パストラバーサル防止）"""
        target = self._resolve_safe_path(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found in sandbox: {relative_path}")
        return target.read_text(encoding="utf-8")

    async def write_file(self, relative_path: str, content: str) -> bool:
        """安全なファイル書き込み（パストラバーサル防止）"""
        target = self._resolve_safe_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return True

    async def clean_up(self) -> None:
        """作業ディレクトリの内容を消去"""
        if self.work_dir.exists():
            for child in self.work_dir.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child)
        log.info("[sandbox.local] Cleaned up sandbox at %s", self.work_dir)

    def _resolve_safe_path(self, relative_path: str) -> Path:
        """パスを正規化し、作業ディレクトリ外の走査を遮断"""
        target = (self.work_dir / relative_path).resolve()
        try:
            target.relative_to(self.work_dir)
        except ValueError:
            raise PermissionError(f"Access denied: Path '{relative_path}' escapes sandbox root.")
        return target
