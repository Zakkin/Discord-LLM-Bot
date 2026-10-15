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


def _extract_json_objects_from_text(text: str) -> list[tuple[str, Any]]:
    """文字列中からネストを考慮して完全なJSONオブジェクトまたは配列を抽出する。"""
    results: list[tuple[str, Any]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ("{", "["):
            closing = "}" if ch == "{" else "]"
            depth = 0
            in_str = False
            escape = False
            start_idx = i
            valid = False
            for j in range(start_idx, n):
                c = text[j]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"':
                    in_str = not in_str
                    continue
                if not in_str:
                    if c == ch:
                        depth += 1
                    elif c == closing:
                        depth -= 1
                        if depth == 0:
                            candidate = text[start_idx : j + 1]
                            try:
                                parsed = json.loads(candidate)
                                results.append((candidate, parsed))
                                valid = True
                                i = j
                            except Exception:
                                pass
                            break
            if not valid:
                i += 1
        else:
            i += 1
    return results


def _parse_function_call_args(args_str: str, tool_name: str = "") -> dict[str, Any]:
    """Action: tool(query='...') 形式などの引数文字列を辞書へパースする。"""
    s = args_str.strip()
    if not s:
        return {}

    # JSONオブジェクト形式の場合
    if s.startswith("{") and s.endswith("}"):
        try:
            res = json.loads(s)
            if isinstance(res, dict):
                return res
        except Exception:
            pass

    # key=value 形式 (例: query='すげえゴイスー 意味', limit=5)
    kv_pattern = re.compile(r"([a-zA-Z0-9_\-]+)\s*=\s*(?:'([^']*)'|\"([^\"]*)\"|([^,\)]+))")
    matches = kv_pattern.findall(s)
    if matches:
        args_dict: dict[str, Any] = {}
        for k, v_single, v_double, v_raw in matches:
            val = v_single if v_single != "" else (v_double if v_double != "" else v_raw.strip())
            # 数値や真偽値の解釈
            if val.lower() == "true":
                args_dict[k] = True
            elif val.lower() == "false":
                args_dict[k] = False
            elif val.isdigit():
                args_dict[k] = int(val)
            else:
                args_dict[k] = val
        return args_dict

    # 単一引用符または二重引用符で囲まれた単一引数 (例: 'すげえゴイスー 意味')
    quoted_match = re.match(r"^['\"](.*)['\"]$", s, re.DOTALL)
    if quoted_match:
        val = quoted_match.group(1)
        key = "query" if "search" in tool_name.lower() or "web" in tool_name.lower() else "input"
        return {key: val}

    # クォート無しの単一文字列
    key = "query" if "search" in tool_name.lower() or "web" in tool_name.lower() else "input"
    return {key: s}


def parse_tool_calls(text: str, registry: Any = None) -> list[ParsedToolCall]:
    """LLMの出力からツール呼び出し指示（Action）を抽出し、MCPか否かを判定して返す。"""
    if not text or not text.strip():
        return []

    calls: list[ParsedToolCall] = []
    raw = text.strip()
    seen_calls: set[str] = set()

    # 共通の思考（Thought）の抽出
    default_thought = ""
    thought_match = re.search(r"Thought:\s*(.*?)(?=\nAction:|\n<tool_call>|```|\Z)", raw, flags=re.DOTALL | re.IGNORECASE)
    if thought_match:
        default_thought = thought_match.group(1).strip()
    else:
        think_tag_match = re.search(r"<think>(.*?)</think>", raw, flags=re.DOTALL | re.IGNORECASE)
        if think_tag_match:
            default_thought = think_tag_match.group(1).strip()

    def _add_call_candidate(
        t_name: str,
        t_args: dict[str, Any] | Any,
        thought: str,
        raw_source: str,
        call_id: str = "",
    ) -> None:
        t_name_clean = str(t_name or "").strip()
        if not t_name_clean:
            return
        args_dict = t_args if isinstance(t_args, dict) else {"input": t_args}
        # 重複登録ガード
        dedup_key = f"{t_name_clean}:{json.dumps(args_dict, sort_keys=True, ensure_ascii=False)}"
        if dedup_key in seen_calls:
            return
        seen_calls.add(dedup_key)

        calls.append(
            ParsedToolCall(
                name=t_name_clean,
                arguments=args_dict,
                thought=thought or default_thought,
                is_mcp=is_mcp_tool(t_name_clean, registry),
                call_id=call_id,
                raw_source=raw_source,
            )
        )

    # 1. <tool_call> タグのパース（Qwen 2.5 / Qwen 3 系標準）
    tool_tag_matches = list(re.finditer(r"<tool_call>\s*(.*?)(?:</tool_call>|\Z)", raw, flags=re.DOTALL | re.IGNORECASE))
    for tm in tool_tag_matches:
        block_content = tm.group(1).strip()
        extracted_jsons = _extract_json_objects_from_text(block_content)
        if extracted_jsons:
            for _, data in extracted_jsons:
                if isinstance(data, dict):
                    th = str(data.get("thought") or "")
                    if "name" in data and ("arguments" in data or "parameters" in data):
                        name = str(data["name"])
                        args_data = data.get("arguments") or data.get("parameters") or {}
                        if isinstance(args_data, str):
                            try:
                                args_data = json.loads(args_data)
                            except Exception:
                                args_data = {"input": args_data}
                        _add_call_candidate(name, args_data, th, tm.group(0))
                    elif "tool" in data:
                        name = str(data["tool"])
                        args_data = data.get("arguments", {})
                        _add_call_candidate(name, args_data, th, tm.group(0))
                    elif "tool_calls" in data and isinstance(data["tool_calls"], list):
                        for tc in data["tool_calls"]:
                            if isinstance(tc, dict):
                                fn = tc.get("function") or {}
                                n = str(fn.get("name") or tc.get("name") or "")
                                a = fn.get("arguments") or tc.get("arguments") or {}
                                _add_call_candidate(n, a, th, tm.group(0), call_id=str(tc.get("id") or ""))

    # 2. 関数呼び出し記法 (Action: search_web(query='...') / Tool: search_web(...) 等)
    fn_call_matches = list(re.finditer(
        r"(?:Action|Tool|Call):\s*([a-zA-Z0-9_\-]+)\s*\((.*?)\)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    ))
    for fm in fn_call_matches:
        tool_name = fm.group(1).strip()
        args_raw = fm.group(2).strip()
        parsed_args = _parse_function_call_args(args_raw, tool_name=tool_name)
        _add_call_candidate(tool_name, parsed_args, default_thought, fm.group(0))

    # 3. 従来の ReAct 形式 (Action: tool_name\nAction Input: ...)
    react_matches = list(re.finditer(
        r"Action:\s*([a-zA-Z0-9_\-]+)\s*\nAction Input:\s*(\{.*?\}|\[.*?\]|[^\n]+)",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    ))
    for rm in react_matches:
        tool_name = rm.group(1).strip()
        arg_str = rm.group(2).strip()
        args: dict[str, Any] = {}
        try:
            parsed_args = json.loads(arg_str)
            if isinstance(parsed_args, dict):
                args = parsed_args
            else:
                args = {"input": parsed_args}
        except Exception:
            key = "query" if "search" in tool_name.lower() or "web" in tool_name.lower() else "input"
            args = {key: arg_str}
        _add_call_candidate(tool_name, args, default_thought, rm.group(0))

    # 4. Markdown コードブロック (```json ... ```) および本文中埋め込みJSON
    json_candidates = _extract_json_objects_from_text(raw)
    for raw_block, data in json_candidates:
        if isinstance(data, dict):
            thought = str(data.get("thought") or "")

            # A. 単一のツール指定形式: {"tool": "...", "arguments": {...}}
            if "tool" in data:
                tool_name = str(data["tool"])
                args_data = data.get("arguments", {})
                arguments = args_data if isinstance(args_data, dict) else {"input": args_data}
                _add_call_candidate(tool_name, arguments, thought, raw_block)
                continue

            # B. OpenAI / Ollama 互換形式: {"name": "...", "arguments": {...}}
            if "name" in data and ("arguments" in data or "parameters" in data):
                tool_name = str(data["name"])
                args_data = data.get("arguments") or data.get("parameters") or {}
                if isinstance(args_data, str):
                    try:
                        args_data = json.loads(args_data)
                    except Exception:
                        args_data = {"input": args_data}
                arguments = args_data if isinstance(args_data, dict) else {"input": args_data}
                _add_call_candidate(tool_name, arguments, thought, raw_block)
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
                            _add_call_candidate(
                                t_name,
                                t_args,
                                thought,
                                raw_block,
                                call_id=str(tc.get("id") or ""),
                            )
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    th = str(item.get("thought") or "")
                    if "name" in item and ("arguments" in item or "parameters" in item):
                        n = str(item["name"])
                        a = item.get("arguments") or item.get("parameters") or {}
                        _add_call_candidate(n, a, th, raw_block)

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
    first_thought = calls[0].thought if calls else ""

    if not first_thought:
        thought_match = re.search(r"Thought:\s*(.*?)(?=\nAction:|\n<tool_call>|```|\Z)", text, flags=re.DOTALL | re.IGNORECASE)
        if thought_match:
            first_thought = thought_match.group(1).strip()
        else:
            think_tag_match = re.search(r"<think>(.*?)(?:</think>|\Z)", text, flags=re.DOTALL | re.IGNORECASE)
            if think_tag_match:
                first_thought = think_tag_match.group(1).strip()

    cleaned_reply = text

    # <tool_call> ... </tool_call> の除去
    cleaned_reply = re.sub(r"<tool_call>[\s\S]*?(?:</tool_call>|\Z)", "", cleaned_reply, flags=re.IGNORECASE)

    # <think> ... </think> の除去（未完タグ含む）
    cleaned_reply = re.sub(r"<think>[\s\S]*?(?:</think>|\Z)", "", cleaned_reply, flags=re.IGNORECASE)

    # Markdownコードブロック ```json ... ``` 全体を除去
    cleaned_reply = re.sub(r"```(?:json)?\s*[\s\S]*?```", "", cleaned_reply, flags=re.IGNORECASE)

    # Action: tool(...) 関数呼び出しの除去
    cleaned_reply = re.sub(r"(?:Action|Tool|Call):\s*[a-zA-Z0-9_\-]+\s*\(.*?\)", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)

    # call.raw_source の除去
    for call in calls:
        if call.raw_source and call.raw_source in cleaned_reply:
            cleaned_reply = cleaned_reply.replace(call.raw_source, "")

    # ReAct表記も除去
    cleaned_reply = re.sub(r"Thought:\s*.*?(?=\nAction:|\n\n|\Z)", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)
    cleaned_reply = re.sub(r"Action:\s*.*?\nAction Input:\s*.*?(?=\n\n|\Z)", "", cleaned_reply, flags=re.DOTALL | re.IGNORECASE)
    cleaned_reply = re.sub(r"\n{3,}", "\n\n", cleaned_reply)
    cleaned_reply = cleaned_reply.strip()

    # 未完思考からのセリフ救出（ツール呼び出しが無く、かつ思考タグ除去後に空になった場合、未完思考内部にセリフが埋もれていないかサルベージ）
    if not calls and not cleaned_reply and ("<think>" in text or "</think>" in text):
        from .salvage import salvage_reply_from_think_block
        salvaged = salvage_reply_from_think_block(text)
        if salvaged:
            cleaned_reply = salvaged.strip()

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
