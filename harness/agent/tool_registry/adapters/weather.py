"""AIエージェント・ハーネス 天気情報ツールアダプター (adapters/weather.py)

天気予報・気象データ取得機能を ToolDefinition として提供するアダプター。
"""
from __future__ import annotations

import logging
from typing import Any

from ..types import ToolCategory, ToolDefinition, ToolParameter

log = logging.getLogger("harness.agent.tool_registry.adapters.weather")


class WeatherToolAdapter:
    """天気情報取得ツールのアダプター"""

    @classmethod
    def create_tool(cls, name: str = "get_weather", timeout_sec: float = 20.0) -> ToolDefinition:
        """get_weather ツール定義を作成する"""
        return ToolDefinition(
            name=name,
            description="指定した地域の現在の天気や天気予報を取得します。",
            parameters=[
                ToolParameter(
                    name="place",
                    type="string",
                    description="都道府県・市区町村名（例: '東京', '大阪', '札幌'）",
                    required=True,
                )
            ],
            handler=cls.execute_weather,
            category=ToolCategory.WEATHER,
            tags={"weather", "forecast"},
            timeout_sec=timeout_sec,
        )

    @staticmethod
    async def execute_weather(place: str, **kwargs: Any) -> str:
        """天気情報を取得し、要約テキストを返す"""
        log.info("[tool_registry] Invoking WeatherToolAdapter place=%r", place)
        try:
            from ..mcp.weather import JapanWeatherService
            service = JapanWeatherService()
            forecast = await service.get_jma_forecast(place)
            if forecast and (forecast.forecasts or forecast.overview):
                return forecast.to_formatted_text()
            return f"{place} の天気情報を取得できませんでした。"
        except Exception as e:
            log.warning("[tool_registry] WeatherToolAdapter error: %s", e)
            return f"天気情報の取得中にエラーが発生しました: {e}"
