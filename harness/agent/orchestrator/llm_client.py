"""AIエージェント・ハーネス LLMクライアント抽象レイヤー (llm_client.py)

オーケストレーターからLLM（Ollama等）へのリクエスト送信、
Function Callingスキーマの注入、思考モード（think）の制御を担当する。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger("harness.agent.orchestrator.llm_client")


class LLMClientProtocol(Protocol):
    """LLM呼び出しクライアントの共通プロトコル"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        think: bool = False,
        temperature: float = 0.4,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """チャットリクエストを送信し、生成結果辞書を返す"""
        ...


class DefaultLLMClient:
    """Ollamaおよびローカル推論モデル向けデフォルトクライアント"""

    def __init__(
        self,
        custom_chat_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.custom_chat_fn = custom_chat_fn
        self.log = logger or log

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        think: bool = False,
        temperature: float = 0.4,
        timeout_sec: float = 60.0,
    ) -> dict[str, Any]:
        """メッセージリストをモデルへ送信し、レスポンスを取得"""
        if self.custom_chat_fn is not None:
            return await self.custom_chat_fn(
                messages=messages,
                tools=tools,
                model=model,
                think=think,
                temperature=temperature,
                timeout_sec=timeout_sec,
            )

        # 既存の ollama_bot 内部の共通関数との連携を試行
        try:
            from ollama_bot.common.ollama import call_ollama_chat_raw
            res = await call_ollama_chat_raw(
                messages,
                model=model,
                think=think,
                tools=tools,
                temperature=temperature,
                timeout_sec=timeout_sec,
            )
            return dict(res) if isinstance(res, dict) else {"message": {"role": "assistant", "content": str(res)}}
        except (ImportError, Exception) as e:
            self.log.warning("[llm_client] Falling back to default mock/stub: %s", e)
            # フォールバックスタブ
            last_msg = messages[-1]["content"] if messages else ""
            return {
                "message": {
                    "role": "assistant",
                    "content": f"Received: {last_msg}",
                },
                "total_duration": 0.1,
                "prompt_eval_count": 50,
                "eval_count": 20,
            }
