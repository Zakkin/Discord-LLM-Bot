"""国内気象情報 MCP パッケージ (harness.agent.tool_registry.mcp.weather)

気象庁 (JMA: https://www.jma.go.jp) および ウェザーニュース (Weathernews: https://weathernews.jp)
から国内の気象情報（予報、概況、警報・注意報、地震、トピックス）を取得する
Model Context Protocol (MCP) サーバーおよびクライアント機能を提供する。
"""
from __future__ import annotations

from .client import WeatherMCPClient
from .jma_client import JMAClient
from .server import WeatherMCPServer, app as weather_mcp_app
from .service import JapanWeatherService, fetch_weather_summary
from .types import (
    ComprehensiveWeatherReport,
    DailyForecast,
    EarthquakeInfo,
    JMAForecastResult,
    JMAWarningResult,
    WNITopicItem,
    clean_jma_weather_text,
    resolve_jma_area_code,
)
from .weathernews_client import WeathernewsClient

__all__ = [
    # Server & Client
    "WeatherMCPServer",
    "WeatherMCPClient",
    "weather_mcp_app",
    # Service & Clients
    "JapanWeatherService",
    "fetch_weather_summary",
    "JMAClient",
    "WeathernewsClient",
    # Types & Models
    "ComprehensiveWeatherReport",
    "DailyForecast",
    "EarthquakeInfo",
    "JMAForecastResult",
    "JMAWarningResult",
    "WNITopicItem",
    "clean_jma_weather_text",
    "resolve_jma_area_code",
]
