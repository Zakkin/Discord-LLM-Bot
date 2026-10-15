"""AIエージェント・ハーネス 観察パーサー (parser.py)

フェーズ4. 結果の観察と評価（Observation）の入力解析を担当。
ツール（フェーズ3）から返却される多様な実行結果：
- 標準ツール結果 (ToolResult)
- Model Context Protocol (MCP) の実行結果 (MCPToolCallResult, text/content blocks)
- JSON-RPC / MCP プロトコルエラー
- HTTP エラー (4xx, 5xx)
- Python 例外オブジェクト (Exception, TimeoutError, etc.)
- 構造化データ (dict, list, raw string)
を包括的かつ堅牢にパースし、正規化された Observation オブジェクトへ変換する。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .types import Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.parser")

# ログインウォール・スクレイピング遮断検知パターン
_LOGIN_WALL_PATTERNS = [
    re.compile(r"login\s*to\s*view", re.IGNORECASE),
    re.compile(r"sign\s*in\s*to\s*(?:twitter|x|continue)", re.IGNORECASE),
    re.compile(r"ログインして(?:ください|表示)", re.IGNORECASE),
    re.compile(r"アカウントを作成", re.IGNORECASE),
    re.compile(r"something went wrong\. try reloading", re.IGNORECASE),
    re.compile(r"cookie\s*(?:consent|policy)", re.IGNORECASE),
]

# レートリミット検知パターン (HTTP 429等)
_RATE_LIMIT_PATTERNS = [
    re.compile(r"rate\s*limit\s*exceeded", re.IGNORECASE),
    re.compile(r"too\s*many\s*requests", re.IGNORECASE),
    re.compile(r"429\s*Too\s*Many\s*Requests", re.IGNORECASE),
    re.compile(r"429\b"),
    re.compile(r"利用上限に達しました", re.IGNORECASE),
]

# 権限制限・サンドボックス拒否パターン (HTTP 403等)
_PERMISSION_DENIED_PATTERNS = [
    re.compile(r"permission\s*denied", re.IGNORECASE),
    re.compile(r"access\s*denied", re.IGNORECASE),
    re.compile(r"forbidden", re.IGNORECASE),
    re.compile(r"403\s*Forbidden", re.IGNORECASE),
    re.compile(r"403\b"),
    re.compile(r"権限がありません", re.IGNORECASE),
    re.compile(r"サンドボックス制限", re.IGNORECASE),
]

# タイムアウト検知パターン (HTTP 504 / 408等)
_TIMEOUT_PATTERNS = [
    re.compile(r"timed\s*out", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"504\s*Gateway\s*Timeout", re.IGNORECASE),
    re.compile(r"タイムアウト", re.IGNORECASE),
]

# サーバーエラー検知パターン (HTTP 500, 502, 503等)
_SERVER_ERROR_PATTERNS = [
    re.compile(r"500\s*Internal\s*Server\s*Error", re.IGNORECASE),
    re.compile(r"502\s*Bad\s*Gateway", re.IGNORECASE),
    re.compile(r"503\s*Service\s*Unavailable", re.IGNORECASE),
    re.compile(r"server\s*error", re.IGNORECASE),
]

# MCP / JSON-RPC プロトコルエラー検知パターン
_PROTOCOL_ERROR_PATTERNS = [
    re.compile(r"json-?rpc", re.IGNORECASE),
    re.compile(r"method\s*not\s*found", re.IGNORECASE),
    re.compile(r"invalid\s*params?", re.IGNORECASE),
    re.compile(r"parse\s*error", re.IGNORECASE),
    re.compile(r"internal\s*rpc\s*error", re.IGNORECASE),
]

# 引数不正検知パターン (HTTP 400等)
_BAD_ARGUMENT_PATTERNS = [
    re.compile(r"400\s*Bad\s*Request", re.IGNORECASE),
    re.compile(r"invalid\s*argument", re.IGNORECASE),
    re.compile(r"validation\s*error", re.IGNORECASE),
    re.compile(r"missing\s*required\s*argument", re.IGNORECASE),
    re.compile(r"引数が不正です", re.IGNORECASE),
]


class ObservationParser:
    """フェーズ3のツール実行結果をパース・正規化する堅牢なパーサー"""

    def __init__(
        self,
        max_output_length: int = 2000,
        logger: logging.Logger | None = None,
    ) -> None:
        self.max_output_length = max_output_length
        self.log = logger or log

    def parse(
        self,
        raw_result: Any,
        tool_name: str = "",
        call_id: str = "",
        execution_time_sec: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> Observation:
        """任意の型（ToolResult, MCPToolCallResult, Exception, dict, str等）を受け取り正規化"""
        meta = dict(metadata or {})

        # 1. オブジェクト型に応じた解体・抽出
        resolved_name = tool_name
        resolved_call_id = call_id
        resolved_success = True
        resolved_output = ""
        resolved_error: str | None = None
        resolved_data: Any = None
        resolved_time = execution_time_sec

        if raw_result is None:
            resolved_output = ""
            resolved_success = True

        elif isinstance(raw_result, Exception):
            resolved_success = False
            resolved_error = f"{type(raw_result).__name__}: {str(raw_result) or repr(raw_result)}"
            resolved_data = raw_result

        elif hasattr(raw_result, "content") and hasattr(raw_result, "text_content"):
            # MCPToolCallResult (Model Context Protocol)
            resolved_name = getattr(raw_result, "name", "") or resolved_name
            resolved_success = bool(getattr(raw_result, "success", True))
            raw_err = getattr(raw_result, "error", None)
            resolved_error = self._format_error(raw_err) if raw_err else None

            # text_content の取得、または content ブロックの抽出
            text_val = getattr(raw_result, "text_content", "") or ""
            if not text_val:
                content_blocks = getattr(raw_result, "content", []) or []
                text_val = self._extract_mcp_content_blocks(content_blocks)

            resolved_output = text_val
            resolved_data = getattr(raw_result, "raw", None) or getattr(raw_result, "content", None)
            meta["is_mcp"] = True

        elif hasattr(raw_result, "output") and hasattr(raw_result, "success"):
            # ToolResult 互換
            resolved_name = getattr(raw_result, "name", "") or resolved_name
            resolved_call_id = getattr(raw_result, "call_id", "") or resolved_call_id
            resolved_success = bool(getattr(raw_result, "success", False))
            resolved_output = self._format_data_to_str(getattr(raw_result, "output", ""))
            raw_err = getattr(raw_result, "error", None)
            resolved_error = self._format_error(raw_err) if raw_err else None
            resolved_data = getattr(raw_result, "data", None)
            resolved_time = float(getattr(raw_result, "execution_time_sec", 0.0)) or resolved_time

        elif isinstance(raw_result, dict):
            # JSON-RPC / MCP エラーレスポンスまたは一般的な辞書
            if "error" in raw_result and ("jsonrpc" in raw_result or "code" in raw_result.get("error", {})):
                err_dict = raw_result["error"]
                resolved_success = False
                if isinstance(err_dict, dict):
                    resolved_error = f"JSON-RPC Error ({err_dict.get('code', 'unknown')}): {err_dict.get('message', '')}"
                else:
                    resolved_error = f"JSON-RPC Error: {err_dict}"
                resolved_data = raw_result
                meta["is_jsonrpc_error"] = True
            else:
                resolved_output = self._format_data_to_str(raw_result)
                resolved_data = raw_result

        elif isinstance(raw_result, (list, tuple, set)):
            resolved_output = self._format_data_to_str(raw_result)
            resolved_data = raw_result

        else:
            resolved_output = str(raw_result)
            resolved_data = raw_result

        # 2. 状態判定 (Status Resolution)
        status = self._detect_status(
            success=resolved_success,
            output=resolved_output,
            error=resolved_error,
        )

        # 3. 出力のサニタイズと文字数制限
        cleaned_output = self.sanitize_raw_output(resolved_output)
        if len(cleaned_output) > self.max_output_length:
            self.log.info(
                "[parser] Truncating output for tool=%s from %d to %d chars",
                resolved_name,
                len(cleaned_output),
                self.max_output_length,
            )
            cleaned_output = (
                cleaned_output[: self.max_output_length]
                + f"\n... [出力が長すぎるため省略されました（計{len(cleaned_output)}文字）]"
            )

        final_success = resolved_success and status not in (
            ObservationStatus.ERROR,
            ObservationStatus.TIMEOUT,
            ObservationStatus.PERMISSION_DENIED,
            ObservationStatus.RATE_LIMITED,
            ObservationStatus.PROTOCOL_ERROR,
            ObservationStatus.SERVER_ERROR,
            ObservationStatus.BAD_ARGUMENTS,
        )

        self.log.info(
            "[parser] Observed tool=%s status=%s success=%s elapsed=%.2fs",
            resolved_name or "unknown",
            status.value,
            final_success,
            resolved_time,
        )

        return Observation(
            tool_name=resolved_name or "unknown_tool",
            call_id=resolved_call_id,
            success=final_success,
            status=status,
            output=cleaned_output,
            error=resolved_error,
            data=resolved_data,
            execution_time_sec=resolved_time,
            metadata=meta,
        )

    def process_tool_result(
        self,
        tool_name: str,
        success: bool,
        output: str = "",
        error: str | None = None,
        data: Any = None,
        call_id: str = "",
        execution_time_sec: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> Observation:
        """既存 ObservationProcessor との完全互換メソッド"""
        from ..tool_registry.types import ToolResult
        t_res = ToolResult(
            name=tool_name,
            call_id=call_id,
            success=success,
            output=output,
            error=error,
            data=data,
            execution_time_sec=execution_time_sec,
        )
        return self.parse(
            raw_result=t_res,
            tool_name=tool_name,
            call_id=call_id,
            execution_time_sec=execution_time_sec,
            metadata=metadata,
        )

    def process_from_tool_registry_result(self, tool_result: Any) -> Observation:
        """既存 ObservationProcessor との完全互換メソッド"""
        return self.parse(raw_result=tool_result)

    def _extract_mcp_content_blocks(self, content_blocks: list[Any]) -> str:
        """MCPの content 配列 ([{'type': 'text', 'text': ...}]) からテキストを抽出"""
        texts: list[str] = []
        for block in content_blocks:
            if isinstance(block, dict):
                b_type = block.get("type")
                if b_type == "text" and "text" in block:
                    texts.append(str(block["text"]))
                elif b_type == "image":
                    texts.append("[MCP Image Content]")
                elif "text" in block:
                    texts.append(str(block["text"]))
                else:
                    texts.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(block, str):
                texts.append(block)
            else:
                texts.append(str(block))
        return "\n".join(texts)

    def sanitize_raw_output(self, text: str) -> str:
        """生ログ、HTML、長大スタックトレース等を要約・サニタイズしてコンテキスト肥大化を防ぐ。"""
        if not text:
            return ""

        s = text

        # 1. base64 / インライン画像の短縮 (data:...;base64,... 形式)
        s = re.sub(
            r"data:[a-zA-Z0-9_\-+.]+/[a-zA-Z0-9_\-+.]+;base64,[A-Za-z0-9+/=]{40,}",
            "[base64 data omitted]",
            s,
        )

        # 2. HTMLタグ・スクリプトのクレンジング (明らかなHTML構造が含まれる場合)
        if re.search(r"<(?:html|body|script|style|div|p|head)\b", s, re.IGNORECASE):
            # script, styleタグを中身ごと削除
            s = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", s, flags=re.IGNORECASE)
            s = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", s, flags=re.IGNORECASE)
            s = re.sub(r"<!--[\s\S]*?-->", "", s)
            # blockタグを改行に変換
            s = re.sub(r"</?(?:p|div|br|h[1-6]|li|tr)[^>]*>", "\n", s, flags=re.IGNORECASE)
            # その他全タグを除去
            s = re.sub(r"<[^>]+>", "", s)

        # 3. 長大な Python Traceback / スタックトレースの圧縮
        if "Traceback (most recent call last):" in s:
            lines = s.splitlines()
            tb_start = -1
            for idx, line in enumerate(lines):
                if "Traceback (most recent call last):" in line:
                    tb_start = idx
                    break
            if tb_start != -1 and len(lines) - tb_start > 12:
                prefix = lines[:tb_start + 3]
                suffix = lines[-4:]
                omitted_count = len(lines) - tb_start - 7
                lines = prefix + [f"    ... [中略: スタックトレース {omitted_count} 行を省略] ..."] + suffix
                s = "\n".join(lines)

        # 4. 冗長な空行・連続空白の圧縮
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = re.sub(r"[ \t]+$", "", s, flags=re.MULTILINE)
        s = re.sub(r"\n{3,}", "\n\n", s)

        return s.strip()

    def _format_error(self, err: Any) -> str:
        """エラー情報を安全に文字列化・サニタイズ"""
        if isinstance(err, Exception):
            raw = f"{type(err).__name__}: {str(err)}"
        elif isinstance(err, dict):
            try:
                raw = json.dumps(err, ensure_ascii=False)
            except Exception:
                raw = str(err)
        else:
            raw = str(err)
        return self.sanitize_raw_output(raw)

    def _format_data_to_str(self, val: Any) -> str:
        """任意のデータを安全に文字列化"""
        if val is None:
            return ""
        if isinstance(val, str):
            return val
        if isinstance(val, (dict, list, tuple)):
            try:
                raw = json.dumps(val, ensure_ascii=False, indent=2)
                return self.sanitize_raw_output(raw)
            except Exception:
                return str(val)
        return str(val)

    def _detect_status(
        self,
        success: bool,
        output: str,
        error: str | None,
    ) -> ObservationStatus:
        """エラーや出力内容から精緻な ObservationStatus を判定"""
        combined = f"{output} {error or ''}".strip()

        # 1. タイムアウト
        if error and any(p.search(error) for p in _TIMEOUT_PATTERNS):
            return ObservationStatus.TIMEOUT
        if any(p.search(combined) for p in _TIMEOUT_PATTERNS):
            return ObservationStatus.TIMEOUT

        # 2. レート制限 (HTTP 429)
        if any(p.search(combined) for p in _RATE_LIMIT_PATTERNS):
            return ObservationStatus.RATE_LIMITED

        # 3. 権限制限 / サンドボックス拒否 (HTTP 403)
        if any(p.search(combined) for p in _PERMISSION_DENIED_PATTERNS):
            return ObservationStatus.PERMISSION_DENIED

        # 4. ログインウォール
        if any(p.search(combined) for p in _LOGIN_WALL_PATTERNS):
            return ObservationStatus.LOGIN_WALL

        # 5. MCP / JSON-RPC プロトコルエラー
        if error and any(p.search(error) for p in _PROTOCOL_ERROR_PATTERNS):
            return ObservationStatus.PROTOCOL_ERROR

        # 6. 引数バリデーションエラー (HTTP 400)
        if error and any(p.search(error) for p in _BAD_ARGUMENT_PATTERNS):
            return ObservationStatus.BAD_ARGUMENTS

        # 7. サーバーエラー (HTTP 500, 502, 503)
        if any(p.search(combined) for p in _SERVER_ERROR_PATTERNS):
            return ObservationStatus.SERVER_ERROR

        # 8. 明示的エラー
        if not success or error:
            return ObservationStatus.ERROR

        # 9. 空結果
        if not output or output.strip() in ("", "None", "null", "[]", "{}"):
            return ObservationStatus.EMPTY_RESULT

        return ObservationStatus.SUCCESS


# 後方互換性エイリアス
ObservationProcessor = ObservationParser
