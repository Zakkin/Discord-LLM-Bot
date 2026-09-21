#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MCP_PROTOCOL_VERSION = "2025-03-26"
DEFAULT_MCP_URL = "http://127.0.0.1:12306/mcp"
DEFAULT_SESSION_PATH = "/tmp/discord-ai-bot-chrome-mcp-session"
DEFAULT_REQUIRED_TOOLS = ("chrome_navigate", "chrome_get_web_content")
ALREADY_CONNECTED_EXIT = 20


class MCPConnectError(RuntimeError):
    pass


class MCPTransportAlreadyConnectedError(MCPConnectError):
    pass


def _compact(text: Any) -> str:
    return " ".join(str(text or "").split())


def _is_transport_already_connected(text: Any) -> bool:
    lowered = str(text or "").lower()
    return "already connected to a transport" in lowered or (
        "call close() before connecting" in lowered and "transport" in lowered
    )


def _is_invalid_session(text: Any) -> bool:
    lowered = str(text or "").lower()
    return "invalid mcp request or session" in lowered or (
        "invalid" in lowered and "mcp" in lowered and "session" in lowered
    )


def _decode_body(body: bytes, content_type: str) -> dict[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    if "text/event-stream" in content_type.lower():
        events: list[dict[str, Any]] = []
        current: list[str] = []

        def flush() -> None:
            if not current:
                return
            raw = "\n".join(current).strip()
            current.clear()
            if not raw or raw == "[DONE]":
                return
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return
            if isinstance(parsed, dict):
                events.append(parsed)

        for line in text.splitlines():
            if line.startswith("data:"):
                current.append(line[5:].strip())
            elif not line.strip():
                flush()
        flush()
        return events[-1] if events else {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise MCPConnectError("MCP response was not a JSON object")
    return parsed


def _read_session_id(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def _write_session_id(path: Path, session_id: str) -> None:
    normalized = str(session_id or "").strip()
    if not normalized:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(normalized)
            handle.write("\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


class MCPHTTPProbe:
    def __init__(self, endpoint: str, *, timeout_sec: float) -> None:
        self.endpoint = endpoint
        self.timeout_sec = timeout_sec
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"}:
            raise MCPConnectError(f"unsupported MCP URL scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise MCPConnectError(f"invalid MCP URL: {endpoint}")
        self._parsed = parsed
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        expect_response: bool = True,
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params or {}),
        }
        if expect_response:
            payload["id"] = self._next_id()

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        conn_cls = http.client.HTTPSConnection if self._parsed.scheme == "https" else http.client.HTTPConnection
        port = self._parsed.port
        path = self._parsed.path or "/"
        if self._parsed.query:
            path = f"{path}?{self._parsed.query}"
        conn = conn_cls(self._parsed.hostname, port, timeout=self.timeout_sec)
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            response_body = response.read()
            response_text = response_body.decode("utf-8", errors="replace")
            response_session_id = str(response.getheader("Mcp-Session-Id") or "").strip()
            if response.status >= 400:
                if _is_transport_already_connected(response_text):
                    raise MCPTransportAlreadyConnectedError(
                        f"MCP HTTP {response.status}: {_compact(response_text)}"
                    )
                raise MCPConnectError(f"MCP HTTP {response.status}: {_compact(response_text)}")
            if not expect_response:
                return {}, response_session_id
            decoded = _decode_body(response_body, str(response.getheader("Content-Type") or ""))
            error = decoded.get("error") if isinstance(decoded, dict) else None
            if isinstance(error, dict):
                message = _compact(error.get("message"))
                if _is_transport_already_connected(message):
                    raise MCPTransportAlreadyConnectedError(message)
                raise MCPConnectError(message or f"MCP request failed: {method}")
            return decoded, response_session_id
        finally:
            conn.close()


def _tool_names_from_result(tools_result: dict[str, Any]) -> set[str]:
    tools = tools_result.get("result", {}).get("tools") if isinstance(tools_result, dict) else []
    return {
        str(tool.get("name") or "").strip()
        for tool in list(tools or [])
        if isinstance(tool, dict) and str(tool.get("name") or "").strip()
    }


def _verify_required_tools(tools_result: dict[str, Any], required_tools: tuple[str, ...]) -> set[str]:
    tool_names = _tool_names_from_result(tools_result)
    missing = [tool for tool in required_tools if tool not in tool_names]
    if missing:
        raise MCPConnectError(f"required MCP tools missing: {', '.join(missing)}")
    return tool_names


def run_probe(
    endpoint: str,
    session_path: Path,
    *,
    timeout_sec: float,
    required_tools: tuple[str, ...] = DEFAULT_REQUIRED_TOOLS,
) -> str:
    probe = MCPHTTPProbe(endpoint, timeout_sec=timeout_sec)
    session_id = _read_session_id(session_path)

    if session_id:
        try:
            tools_result, _ = probe.request("tools/list", {}, session_id=session_id)
            tool_names = _verify_required_tools(tools_result, required_tools)
            return f"ok: reused saved MCP session {session_id}; tools={len(tool_names)}"
        except MCPConnectError as e:
            if not _is_invalid_session(str(e)):
                raise
            session_id = ""

    result, response_session_id = probe.request(
        "initialize",
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "discord-ai-bot-startup-check", "version": "1.0"},
        },
    )
    session_id = response_session_id
    if not session_id:
        raise MCPConnectError(f"MCP initialize did not return Mcp-Session-Id: {result!r}")
    _write_session_id(session_path, session_id)
    probe.request("notifications/initialized", {}, session_id=session_id, expect_response=False)
    tools_result, _ = probe.request("tools/list", {}, session_id=session_id)
    tool_names = _verify_required_tools(tools_result, required_tools)
    return f"ok: initialized MCP session {session_id}; tools={len(tool_names)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check mcp-chrome Streamable HTTP connectivity.")
    parser.add_argument("--endpoint", default=os.environ.get("WEB_RESEARCH_MCP_URL", DEFAULT_MCP_URL))
    parser.add_argument(
        "--session-path",
        default=os.environ.get("WEB_RESEARCH_MCP_SESSION_PATH", DEFAULT_SESSION_PATH),
    )
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("WEB_RESEARCH_MCP_TIMEOUT_SEC", "20")))
    parser.add_argument(
        "--required-tool",
        action="append",
        default=[],
        help="MCP tool name that must be present. Defaults to bot browser-read tools.",
    )
    args = parser.parse_args(argv)
    required_tools = tuple(args.required_tool or DEFAULT_REQUIRED_TOOLS)

    try:
        message = run_probe(
            args.endpoint,
            Path(args.session_path),
            timeout_sec=args.timeout_sec,
            required_tools=required_tools,
        )
    except MCPTransportAlreadyConnectedError as e:
        print(f"mcp-chrome protocol check failed: {e}", file=sys.stderr)
        print(
            "A previous MCP transport is still connected, but no reusable session id is available. "
            "Restart the bot Chrome profile or avoid deleting the session file.",
            file=sys.stderr,
        )
        return ALREADY_CONNECTED_EXIT
    except Exception as e:
        print(f"mcp-chrome protocol check failed: {e}", file=sys.stderr)
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
