"""AIエージェント・ハーネス アクションパース＆MCP連携モジュール (parser/actions.py)

フェーズ2（推論と計画立案）でLLMが出力した関数呼び出し計画をパース・解釈し、
フェーズ3（ツール選択と実行）においてオーケストレーターがMCPサーバーやローカルツールへ
安全に干渉・リクエスト送信できるようにする。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("harness.agent.prompt_parser.parser.actions")


@dataclass
class ParsedToolCall:
    """LLMの思考と共に出力されたツール呼び出し計画"""
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    thought: str = ""
    is_mcp: bool = False
    call_id: str = ""
    raw_source: str = ""

    def to_tool_call(self) -> Any:
        """ToolRegistry の ToolCall データクラスへ変換"""
        try:
            from harness.agent.tool_registry.types import ToolCall
            return ToolCall(name=self.name, arguments=self.arguments, call_id=self.call_id)
        except ImportError:
            return None


def is_mcp_tool(tool_name: str, registry: Any = None) -> bool:
    """指定されたツールがMCP経由のものであるかを判定する"""
    name = (tool_name or "").strip()
    if not name:
        return False

    # 1. レジストリが提供されている場合はカテゴリ・タグを確認
    if registry is not None:
        tool_def = None
        if hasattr(registry, "get"):
            tool_def = registry.get(name)
        elif hasattr(registry, "tools") and isinstance(registry.tools, dict):
            tool_def = registry.tools.get(name)

        if tool_def is not None:
            cat = getattr(tool_def, "category", None)
            cat_str = str(getattr(cat, "value", cat) or "").lower()
            if cat_str == "mcp":
                return True
            tags: set[str] | list[str] = getattr(tool_def, "tags", set())
            if "mcp" in tags:
                return True

    # 2. プレフィックスおよび既知のMCPツール命名規則から判定
    mcp_prefixes = (
        "mcp_",
        "mcp-",
        "jma_",
        "weathernews_",
        "japan_weather",
        "chrome_",
        "xcom_",
        "img2chan_",
    )
    if any(name.startswith(p) for p in mcp_prefixes):
        return True

    return False


def parse_tool_calls(text: str, registry: Any = None) -> list[ParsedToolCall]:
    """LLMの出力からツール呼び出し指示（Action）を抽出し、MCPか否かを判定して返す。"""
    if not text or not text.strip():
        return []

    calls: list[ParsedToolCall] = []
    raw = text.strip()

    # 1. ReAct形式のパース:
    # Thought: ...
    # Action: tool_name
    # Action Input: {...}
    react_thought = ""
    thought_match = re.search(r"Thought:\s*(.*?)(?=\nAction:|\Z)", raw, flags=re.DOTALL | re.IGNORECASE)
    if thought_match:
        react_thought = thought_match.group(1).strip()

    action_match = re.search(
        r"Action:\s*([a-zA-Z0-9_\-]+)\s*\nAction Input:\s*(\{.*?\}|\[.*?\]|[^\n]+)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if action_match:
        tool_name = action_match.group(1).strip()
        arg_str = action_match.group(2).strip()
        args: dict[str, Any] = {}
        try:
            parsed_args = json.loads(arg_str)
            if isinstance(parsed_args, dict):
                args = parsed_args
            else:
                args = {"input": parsed_args}
        except Exception:
            args = {"input": arg_str}

        calls.append(
            ParsedToolCall(
                name=tool_name,
                arguments=args,
                thought=react_thought,
                is_mcp=is_mcp_tool(tool_name, registry),
                raw_source=action_match.group(0),
            )
        )
        log.info(
            "[prompt-manager] Parsed ReAct action: tool=%s is_mcp=%s thought=%r",
            tool_name,
            calls[-1].is_mcp,
            react_thought[:40],
        )
        return calls

    # 2. JSONブロックのパース
    # ```json ... ``` または 生の { ... }
    json_candidates: list[str] = []
    code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
    if code_blocks:
        json_candidates.extend(code_blocks)
    else:
        # コードブロックがない場合、波括弧を抽出
        brace_matches = re.findall(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, flags=re.DOTALL)
        if brace_matches:
            json_candidates.extend(brace_matches)
        elif raw.startswith("{") and raw.endswith("}"):
            json_candidates.append(raw)

    for cand in json_candidates:
        try:
            data = json.loads(cand)
            if not isinstance(data, dict):
                continue

            thought = str(data.get("thought") or "")

            # A. 単一のツール指定形式: {"tool": "...", "arguments": {...}}
            if "tool" in data:
                tool_name = str(data["tool"])
                args_data = data.get("arguments", {})
                arguments = args_data if isinstance(args_data, dict) else {"input": args_data}
                calls.append(
                    ParsedToolCall(
                        name=tool_name,
                        arguments=arguments,
                        thought=thought,
                        is_mcp=is_mcp_tool(tool_name, registry),
                        raw_source=cand,
                    )
                )
                continue

            # B. OpenAI / Ollama 互換形式: {"name": "...", "arguments": {...}}
            if "name" in data and ("arguments" in data or "parameters" in data):
                tool_name = str(data["name"])
                args_data = data.get("arguments") or data.get("parameters") or {}
                arguments = args_data if isinstance(args_data, dict) else {"input": args_data}
                calls.append(
                    ParsedToolCall(
                        name=tool_name,
                        arguments=arguments,
                        thought=thought,
                        is_mcp=is_mcp_tool(tool_name, registry),
                        raw_source=cand,
                    )
                )
                continue

            # C. tool_calls 配列形式: {"tool_calls": [...]}
            if "tool_calls" in data and isinstance(data["tool_calls"], list):
                for tc in data["tool_calls"]:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        t_name = str(fn.get("name") or tc.get("name") or "")
                        t_args_raw = fn.get("arguments") or tc.get("arguments") or {}
                        if isinstance(t_args_raw, str):
                            try:
                                t_args = json.loads(t_args_raw)
                            except Exception:
                                t_args = {"input": t_args_raw}
                        elif isinstance(t_args_raw, dict):
                            t_args = t_args_raw
                        else:
                            t_args = {}
                        if t_name:
                            calls.append(
                                ParsedToolCall(
                                    name=t_name,
                                    arguments=t_args,
                                    thought=thought,
                                    is_mcp=is_mcp_tool(t_name, registry),
                                    call_id=str(tc.get("id") or ""),
                                    raw_source=cand,
                                )
                            )
        except Exception:
            continue

    if calls:
        log.info(
            "[prompt-manager] Extracted %d tool call(s) from output (first tool=%s, is_mcp=%s)",
            len(calls),
            calls[0].name,
            calls[0].is_mcp,
        )

    return calls


def extract_thought_and_action(
    text: str,
    registry: Any = None,
) -> tuple[str, list[ParsedToolCall], str]:
    """出力から思考（thought）、ツール呼び出しリスト（actions）、残りのセリフ（user_reply）を分離する。"""
    calls = parse_tool_calls(text, registry=registry)
    if not calls:
        return "", [], text.strip()

    first_thought = calls[0].thought
    # ツール呼び出しブロックを除去して残りのセリフを取得
    cleaned_reply = text
    # Markdownコードブロック ```json ... ``` 全体を除去
    cleaned_reply = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)
    for call in calls:
        if call.raw_source:
            cleaned_reply = cleaned_reply.replace(call.raw_source, "")

    # ReAct表記も除去
    cleaned_reply = re.sub(r"Thought:.*?(?=\nAction:|\n\n|\Z)", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)
    cleaned_reply = re.sub(r"Action:.*?\nAction Input:.*?(?=\n\n|\Z)", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)
    cleaned_reply = re.sub(r"\n{3,}", "\n\n", cleaned_reply)
    cleaned_reply = cleaned_reply.strip()

    return first_thought, calls, cleaned_reply


async def execute_action(
    call: ParsedToolCall | Any,
    registry: Any = None,
    mcp_client: Any = None,
) -> Any:
    """オーケストレーターが外部環境への干渉を行う実行関数。

    指示されたツールがMCP経由のものである場合、MCPクライアントを介して
    対象のMCPサーバーへ JSON-RPC プロトコルの CallTool を送信し、結果を返却する。
    """
    start_time = time.perf_counter()
    tool_name = getattr(call, "name", "")
    arguments = getattr(call, "arguments", {}) or {}
    call_id = getattr(call, "call_id", "")
    is_mcp = getattr(call, "is_mcp", False) or is_mcp_tool(tool_name, registry)

    log.info(
        "[prompt-manager] Executing action: tool=%s is_mcp=%s arguments=%s",
        tool_name,
        is_mcp,
        arguments,
    )

    # 1. MCP経由のツール呼び出し
    if is_mcp and mcp_client is not None and hasattr(mcp_client, "call_tool"):
        try:
            log.info(
                "[prompt-manager] Dispatching JSON-RPC CallTool to MCP server: tool=%s",
                tool_name,
            )
            raw_result = await mcp_client.call_tool(tool_name, arguments)
            elapsed = time.perf_counter() - start_time

            from harness.agent.tool_registry.types import ToolResult
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                success=True,
                output=str(raw_result) if not isinstance(raw_result, str) else raw_result,
                data=raw_result,
                execution_time_sec=elapsed,
            )
        except Exception as e:
            elapsed = time.perf_counter() - start_time
            log.exception("[prompt-manager] MCP CallTool execution failed: %s", e)
            from harness.agent.tool_registry.types import ToolResult
            return ToolResult(
                name=tool_name,
                call_id=call_id,
                success=False,
                error=f"MCP error: {e}",
                execution_time_sec=elapsed,
            )

    # 2. レジストリ経由のディスパッチ
    if registry is not None and hasattr(registry, "execute"):
        tool_call = call.to_tool_call() if hasattr(call, "to_tool_call") else None
        if tool_call is not None:
            return await registry.execute(tool_call)
        return await registry.execute(tool_name, arguments)

    # 3. レジストリがない場合のフォールバックエラー
    elapsed = time.perf_counter() - start_time
    err = f"No handler or registry available to execute tool: '{tool_name}'"
    log.warning("[prompt-manager] %s", err)
    try:
        from harness.agent.tool_registry.types import ToolResult
        return ToolResult(
            name=tool_name,
            call_id=call_id,
            success=False,
            error=err,
            execution_time_sec=elapsed,
        )
    except ImportError:
        return {"name": tool_name, "success": False, "error": err}
