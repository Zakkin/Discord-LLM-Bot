"""AIエージェント・ハーネス ツール実行エンジン (executor.py)

フェーズ3. ツール選択と実行（Action）を担当し、
LLMから指示されたツール呼び出し（ToolCall）を検証・ルーティング・安全に実行する。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable

from .types import ToolCall, ToolDefinition, ToolResult

log = logging.getLogger("harness.agent.tool_registry.executor")


class ToolExecutor:
    """ツール実行・ルーティング・監視を司る実行エンジン"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    async def execute(
        self,
        tool: ToolDefinition,
        call: ToolCall,
        timeout_sec: float | None = None,
    ) -> ToolResult:
        """単一のツール呼び出しを安全に実行する。

        Args:
            tool: 実行対象のツール定義
            call: 呼び出しパラメータ
            timeout_sec: 個別タイムアウト指定（未指定時はツールの設定値を使用）

        Returns:
            ToolResult: 実行結果（Observation入力）
        """
        start_time = time.perf_counter()
        call_id = call.call_id
        name = tool.name
        effective_timeout = timeout_sec if timeout_sec is not None else tool.timeout_sec

        self.log.info(
            "[tool_registry] Action executing tool=%s call_id=%s timeout=%.1fs arguments=%s",
            name,
            call_id,
            effective_timeout,
            call.arguments,
        )

        if not tool.enabled:
            err_msg = f"Tool '{name}' is disabled."
            self.log.warning("[tool_registry] Action rejected tool=%s: %s", name, err_msg)
            return ToolResult(
                name=name,
                call_id=call_id,
                success=False,
                error=err_msg,
                execution_time_sec=time.perf_counter() - start_time,
            )

        if tool.handler is None:
            err_msg = f"Tool '{name}' has no registered handler."
            self.log.error("[tool_registry] Action failed tool=%s: %s", name, err_msg)
            return ToolResult(
                name=name,
                call_id=call_id,
                success=False,
                error=err_msg,
                execution_time_sec=time.perf_counter() - start_time,
            )

        # パラメータバリデーション
        validation_error = self._validate_arguments(tool, call.arguments)
        if validation_error:
            self.log.warning(
                "[tool_registry] Action validation error tool=%s: %s",
                name,
                validation_error,
            )
            return ToolResult(
                name=name,
                call_id=call_id,
                success=False,
                error=validation_error,
                execution_time_sec=time.perf_counter() - start_time,
            )

        try:
            # タイムアウト付きでハンドラを実行
            raw_output = await asyncio.wait_for(
                self._invoke_handler(tool.handler, call.arguments),
                timeout=effective_timeout,
            )
            elapsed = time.perf_counter() - start_time

            formatted_output = self._format_output(raw_output)
            self.log.info(
                "[tool_registry] Action succeeded tool=%s elapsed=%.2fs output_length=%d",
                name,
                elapsed,
                len(formatted_output),
            )

            return ToolResult(
                name=name,
                call_id=call_id,
                success=True,
                output=formatted_output,
                data=raw_output,
                execution_time_sec=elapsed,
            )

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start_time
            err_msg = f"Tool '{name}' timed out after {effective_timeout:.1f}s"
            self.log.error("[tool_registry] Action timeout tool=%s elapsed=%.2fs", name, elapsed)
            return ToolResult(
                name=name,
                call_id=call_id,
                success=False,
                error=err_msg,
                execution_time_sec=elapsed,
            )
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            err_msg = f"Tool execution error: {type(e).__name__}: {e}"
            self.log.exception("[tool_registry] Action exception tool=%s: %s", name, err_msg)
            return ToolResult(
                name=name,
                call_id=call_id,
                success=False,
                error=err_msg,
                execution_time_sec=elapsed,
            )

    async def _invoke_handler(
        self,
        handler: Callable[..., Any],
        arguments: dict[str, Any],
    ) -> Any:
        """同期・非同期関数を透過的に実行し、可変引数・キーワード引数の不一致を安全に吸収する"""
        sig = inspect.signature(handler)
        bound_args: dict[str, Any] = {}
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )

        for param_name, param in sig.parameters.items():
            if param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                if param_name in arguments:
                    bound_args[param_name] = arguments[param_name]
                elif param.default is not inspect.Parameter.empty:
                    bound_args[param_name] = param.default

        if has_var_keyword:
            for k, v in arguments.items():
                if k not in bound_args:
                    bound_args[k] = v

        if inspect.iscoroutinefunction(handler):
            return await handler(**bound_args)

        result = handler(**bound_args)
        if inspect.isawaitable(result):
            return await result
        return result

    def _validate_arguments(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> str | None:
        """必須パラメータの有無および基本的なバリデーション"""
        for param in tool.parameters:
            if param.required and param.name not in arguments:
                return f"Missing required parameter: '{param.name}'"
            if param.name in arguments and param.enum is not None:
                val = arguments[param.name]
                if val not in param.enum:
                    return f"Parameter '{param.name}' must be one of {param.enum}, got '{val}'"
        return None

    def _format_output(self, output: Any) -> str:
        """ツール実行結果をLLMが理解しやすいテキスト文字列に整形"""
        if output is None:
            return "Success"
        if isinstance(output, str):
            return output
        if isinstance(output, (dict, list, int, float, bool)):
            import json
            try:
                return json.dumps(output, ensure_ascii=False, indent=2)
            except Exception:
                return str(output)
        return str(output)
