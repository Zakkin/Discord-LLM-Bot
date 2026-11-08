"""AIエージェント・ハーネス Dockerコンテナ分離サンドボックス (docker_backend.py)

2-1. サンドボックス実行環境として、
Dockerコンテナを用いた強固なプロセス・ネットワーク・ファイルシステム隔離空間を提供する。
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Sequence

from .interface import SandboxEnvironment
from .types import SandboxBackend, SandboxConfig, SandboxResult

log = logging.getLogger("harness.agent.test_harness.sandbox.docker")


class DockerSandbox(SandboxEnvironment):
    """Dockerコンテナを用いた隔離実行環境"""

    def __init__(
        self,
        config: SandboxConfig,
        image_name: str = "python:3.11-slim",
    ) -> None:
        super().__init__(config)
        self.image_name = image_name
        self._docker_available: bool | None = None

    async def initialize(self) -> None:
        """作業ディレクトリの準備およびDocker環境チェック"""
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self._docker_available = shutil.which("docker") is not None
        if not self._docker_available:
            log.warning("[sandbox.docker] Docker CLI is not installed on host. Running in fallback mode.")
        else:
            log.info("[sandbox.docker] Docker sandbox ready with image '%s'", self.image_name)

    async def execute_command(
        self,
        command: str | Sequence[str],
        timeout_sec: float | None = None,
    ) -> SandboxResult:
        """Dockerコンテナ内で隔離実行"""
        start_time = time.perf_counter()
        effective_timeout = timeout_sec if timeout_sec is not None else self.config.timeout_sec

        if not self._docker_available:
            # Docker非利用環境時の安全な通知
            return SandboxResult(
                exit_code=1,
                stderr="Docker is not available on host system.",
                execution_time_sec=time.perf_counter() - start_time,
                backend=SandboxBackend.DOCKER,
            )

        cmd_args = [command] if isinstance(command, str) else list(command)

        # Docker実行引数の組み立て
        docker_args = [
            "docker", "run", "--rm",
            "-v", f"{self.config.work_dir.resolve()}:/workspace",
            "-w", "/workspace",
            f"--memory={self.config.memory_limit_mb}m",
            f"--cpus={self.config.cpu_limit_cores}",
        ]
        if self.config.network_disabled:
            docker_args.extend(["--network", "none"])

        docker_args.append(self.image_name)
        docker_args.extend(cmd_args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
            elapsed = time.perf_counter() - start_time

            return SandboxResult(
                exit_code=proc.returncode if proc.returncode is not None else 0,
                stdout=stdout_bytes[: self.config.max_output_bytes].decode("utf-8", errors="replace"),
                stderr=stderr_bytes[: self.config.max_output_bytes].decode("utf-8", errors="replace"),
                execution_time_sec=elapsed,
                backend=SandboxBackend.DOCKER,
            )
        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start_time
            return SandboxResult(
                exit_code=-1,
                stderr=f"Docker container timed out after {effective_timeout:.1f}s",
                execution_time_sec=elapsed,
                timed_out=True,
                backend=SandboxBackend.DOCKER,
            )
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            return SandboxResult(
                exit_code=-1,
                stderr=f"Docker execution error: {e}",
                execution_time_sec=elapsed,
                backend=SandboxBackend.DOCKER,
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
