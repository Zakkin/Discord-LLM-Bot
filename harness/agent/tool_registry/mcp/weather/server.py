"""国内気象情報 MCP サーバー (mcp/weather/server.py)

気象庁 (JMA: https://www.jma.go.jp) および ウェザーニュース (Weathernews: https://weathernews.jp)
から取得した気象データを MCP (Model Context Protocol / JSON-RPC 2.0) で提供する。
FastAPI による HTTP /mcp エンドポイントおよびダイレクトディスパッチに対応。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from .service import JapanWeatherService

log = logging.getLogger("harness.agent.tool_registry.mcp.weather.server")

# FastAPI はオプショナルインポート（軽量環境や直接実行に対応）
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


class WeatherMCPServer:
    """国内気象情報 MCP サーバー実装"""

    def __init__(self, service: JapanWeatherService | None = None) -> None:
        self.service = service or JapanWeatherService()

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """MCPプロトコル仕様のツール一覧定義を返却する"""
        return [
            {
                "name": "jma_get_weather_forecast",
                "description": "気象庁 (https://www.jma.go.jp) から指定地域の公式天気予報（今日・明日等の天気、気温、降水確率、概況）を取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area": {
                            "type": "string",
                            "description": "都道府県または主要都市名（例: '東京', '大阪', '札幌', '福岡', '名古屋'）",
                        }
                    },
                    "required": ["area"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "jma_get_weather_warnings",
                "description": "気象庁 (https://www.jma.go.jp) から指定地域の現在発表されている気象警報・注意報（大雨、洪水、暴風等）を取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area": {
                            "type": "string",
                            "description": "都道府県または主要都市名（例: '東京', '神奈川', '愛知'）",
                        }
                    },
                    "required": ["area"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "jma_get_earthquake_info",
                "description": "気象庁 (https://www.jma.go.jp) から最新の地震情報（震源地、最大震度、マグニチュード、津波の有無）を取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "取得する地震情報の件数（デフォルト: 3）",
                            "default": 3,
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wni_get_weather_topics",
                "description": "ウェザーニュース (https://weathernews.jp) から最新の気象トピックス・速報ニュース・解説記事を取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "limit": {
                            "type": "integer",
                            "description": "取得するトピックス件数（デフォルト: 5）",
                            "default": 5,
                        }
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "wni_get_area_weather",
                "description": "ウェザーニュース (https://weathernews.jp) から指定地域の気象状況・解説トピックスを取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area": {
                            "type": "string",
                            "description": "都道府県または都市名（例: '東京', '大阪'）",
                        }
                    },
                    "required": ["area"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "japan_weather_comprehensive",
                "description": "気象庁（公式予報・警報）とウェザーニュース（最新トピック・解説）の双方から情報を取得し、包括的な日本の気象レポートを取得します。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "area": {
                            "type": "string",
                            "description": "対象の地域名（例: '東京', '大阪', '北海道'）",
                        }
                    },
                    "required": ["area"],
                    "additionalProperties": False,
                },
            },
        ]

    async def handle_jsonrpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        """MCP JSON-RPC 2.0 リクエストを処理する"""
        method = payload.get("method")
        req_id = payload.get("id")

        log.info("[tool_registry] Weather MCP request method=%s id=%s", method, req_id)

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "japan-weather-mcp",
                        "version": "1.0.0",
                    },
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": self.get_tool_definitions(),
                },
            }

        elif method == "tools/call":
            params = payload.get("params", {})
            tool_name = params.get("name")
            args = params.get("arguments", {})

            try:
                text_result = await self.execute_tool(tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": text_result}],
                    },
                }
            except Exception as e:
                log.error("[tool_registry] Weather MCP tool execution error %s: %s", tool_name, e)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32000,
                        "message": f"Tool execution failed: {e}",
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method '{method}' not found"},
        }

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        """ツール名と引数を受け取って実行し、テキストを返す"""
        log.info("[tool_registry] Executing weather MCP tool name=%s args=%s", name, args)

        if name == "jma_get_weather_forecast":
            area = str(args.get("area") or "東京")
            forecast = await self.service.get_jma_forecast(area)
            return forecast.to_formatted_text()

        elif name == "jma_get_weather_warnings":
            area = str(args.get("area") or "東京")
            warnings = await self.service.get_jma_warnings(area)
            return warnings.to_formatted_text()

        elif name == "jma_get_earthquake_info":
            limit = int(args.get("limit") or 3)
            quakes = await self.service.get_jma_earthquakes(limit=limit)
            if not quakes:
                return "現在、気象庁発表の直近の地震情報はありません。"
            lines = ["【気象庁公式 最新地震情報】"]
            for q in quakes:
                lines.append(f"- {q.to_formatted_text()}")
            return "\n".join(lines)

        elif name == "wni_get_weather_topics":
            limit = int(args.get("limit") or 5)
            topics = await self.service.get_wni_topics(limit=limit)
            lines = ["【ウェザーニュース 最新気象トピックス】 (https://weathernews.jp)"]
            for t in topics:
                lines.append(t.to_formatted_text())
            return "\n".join(lines)

        elif name == "wni_get_area_weather":
            area = str(args.get("area") or "東京")
            return await self.service.get_wni_area_summary(area)

        elif name == "japan_weather_comprehensive":
            area = str(args.get("area") or "東京")
            report = await self.service.get_comprehensive_report(area)
            return report.to_formatted_text()

        else:
            raise ValueError(f"Unknown weather MCP tool: '{name}'")


# FastAPI アプリケーションインスタンスの生成
server_instance = WeatherMCPServer()
app: Any = None

if HAS_FASTAPI:
    app = FastAPI(title="Japan Weather MCP Server", version="1.0.0")

    @app.post("/mcp")
    @app.post("/")
    async def mcp_endpoint(request: Request) -> Any:
        body = await request.json()
        return await server_instance.handle_jsonrpc(body)


if __name__ == "__main__":
    if HAS_FASTAPI:
        log.info("[tool_registry] Starting Weather MCP Server on port 12307...")
        uvicorn.run(app, host="127.0.0.1", port=12307)
    else:
        print("FastAPI is not installed. Run with Python MCP client directly.")
