"""ファクトチェック MCP サーバー (mcp/fact_check/server.py)

Web検索・URL要約・真偽検証・アクション判定機能を MCP (Model Context Protocol / JSON-RPC 2.0)
で提供するサーバー実装。FastAPI による HTTP /mcp エンドポイントおよびダイレクトディスパッチに対応。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from .service import FactCheckService

log = logging.getLogger("harness.agent.tool_registry.mcp.fact_check.server")

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    import uvicorn
    HAS_FASTAPI = True
else:
    try:
        from fastapi import FastAPI, Request
        import uvicorn
        HAS_FASTAPI = True
    except ImportError:
        HAS_FASTAPI = False
        FastAPI = None
        Request = None
        uvicorn = None


class FactCheckMCPServer:
    """ファクトチェック MCP サーバー実装"""

    def __init__(self, service: FactCheckService | None = None) -> None:
        self.service = service or FactCheckService()

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """MCPプロトコル仕様のツール一覧定義を返却する"""
        return [
            {
                "name": "fact_check_verify",
                "description": "指定された言説・テキストの裏取りWeb検索および真偽検証を行い、信頼度付きの要約と出典URLを返します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "claim": {
                            "type": "string",
                            "description": "検証対象の主張・ニュース・テキスト（URLを含む場合は自動展開されます）",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "検索結果の上限件数（デフォルト: 3）",
                            "default": 3,
                        },
                    },
                    "required": ["claim"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "fact_check_search_web",
                "description": "Web検索を実行し、タイトル・スニペット・URLのリストまたは要約テキストを取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "検索クエリ文字列",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "取得件数（デフォルト: 3）",
                            "default": 3,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "fact_check_extract_context",
                "description": "メッセージ内に含まれるURLやX/Twitter投稿のメタデータを解決し、要約と検索用テキストを抽出します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "URLまたはテキストを含む発話",
                        }
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "fact_check_detect_action",
                "description": "ユーザー発話からファクトチェック関連アクション（fact_check / summarize / what_is_this / simplify）を検出します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "判定対象のユーザー発話テキスト",
                        }
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
        ]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """ツール名と引数を指定してツールを実行する（MCP仕様の dict を返却）"""
        try:
            if tool_name == "fact_check_verify":
                claim = str(arguments.get("claim") or "").strip()
                max_results = int(arguments.get("max_results") or 3)
                if not claim:
                    return {"content": [{"type": "text", "text": "検証対象の claim が指定されていません。"}]}
                res = await self.service.verify_claim(claim, max_results=max_results)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"【ファクトチェック結果】\nステータス: {res.status}\n信頼度: {res.confidence:.1f}\n\n{res.summary}",
                        }
                    ],
                    "metadata": res.to_dict(),
                }

            elif tool_name == "fact_check_search_web":
                query = str(arguments.get("query") or "").strip()
                max_results = int(arguments.get("max_results") or 3)
                if not query:
                    return {"content": [{"type": "text", "text": "検索クエリが指定されていません。"}]}
                text_res = await self.service.search_web(query, max_results=max_results)
                entries = await self.service.search_web_entries(query, max_results=max_results)
                return {
                    "content": [{"type": "text", "text": text_res}],
                    "metadata": {"entries": entries},
                }

            elif tool_name == "fact_check_extract_context":
                text = str(arguments.get("text") or "").strip()
                ctx = await self.service.extract_context(text)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": ctx.enriched_text or "(URLメタデータなし)",
                        }
                    ],
                    "metadata": {
                        "original_text": ctx.original_text,
                        "link_summaries": ctx.link_summaries,
                        "search_query_source_text": ctx.search_query_source_text,
                    },
                }

            elif tool_name == "fact_check_detect_action":
                text = str(arguments.get("text") or "").strip()
                action = await self.service.detect_action(text)
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"アクション: {action}" if action else "該当アクションなし",
                        }
                    ],
                    "metadata": {"action": action},
                }

            else:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"未知のツール名: {tool_name}"}],
                }

        except Exception as e:
            log.warning("FactCheckMCPServer.call_tool error: %s", e)
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"ツール実行エラー ({tool_name}): {e}"}],
            }

    async def handle_jsonrpc(self, request_data: dict[str, Any]) -> dict[str, Any]:
        """JSON-RPC 2.0 リクエストを処理する"""
        req_id = request_data.get("id")
        method = request_data.get("method")
        params = request_data.get("params") or {}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_definitions()},
            }

        elif method == "tools/call":
            tool_name = params.get("name") or ""
            args = params.get("arguments") or {}
            call_res = await self.call_tool(tool_name, args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": call_res,
            }

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }


server_instance = FactCheckMCPServer()
app: Any = None

if HAS_FASTAPI:
    app = FastAPI(title="FactCheck MCP Server", version="1.0.0")

    @app.post("/mcp")
    @app.post("/")
    async def handle_mcp(request: Request) -> Any:
        try:
            data = await request.json()
        except Exception:
            return {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
        return await server_instance.handle_jsonrpc(data)

    @app.get("/tools")
    async def list_tools() -> Any:
        return {"tools": server_instance.get_tool_definitions()}

