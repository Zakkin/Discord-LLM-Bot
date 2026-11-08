"""国内気象情報統合サービス (mcp/weather/service.py)

気象庁 (JMA: https://www.jma.go.jp) と ウェザーニュース (Weathernews: https://weathernews.jp)
の2つのソースを統合し、統一的な気象情報・警報・トピックスを提供する。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

import aiohttp

from .jma_client import JMAClient
from .types import (
    ComprehensiveWeatherReport,
    EarthquakeInfo,
    JMAForecastResult,
    JMAWarningResult,
    WNITopicItem,
)
from .weathernews_client import WeathernewsClient

log = logging.getLogger("harness.agent.tool_registry.mcp.weather.service")


class JapanWeatherService:
    """国内気象情報統合サービス"""

    def __init__(
        self,
        jma_client: JMAClient | None = None,
        wni_client: WeathernewsClient | None = None,
    ) -> None:
        self.jma = jma_client or JMAClient()
        self.wni = wni_client or WeathernewsClient()

    async def get_jma_forecast(self, area: str) -> JMAForecastResult:
        """気象庁公式の天気予報を取得"""
        return await self.jma.get_forecast(area)

    async def get_jma_warnings(self, area: str) -> JMAWarningResult:
        """気象庁公式の気象警報・注意報を取得"""
        return await self.jma.get_warnings(area)

    async def get_jma_earthquakes(self, limit: int = 3) -> list[EarthquakeInfo]:
        """気象庁の最新地震情報を取得"""
        return await self.jma.get_earthquake_info(limit=limit)

    async def get_wni_topics(self, limit: int = 5) -> list[WNITopicItem]:
        """ウェザーニュースの最新気象トピックスを取得"""
        return await self.wni.get_latest_topics(limit=limit)

    async def get_wni_area_summary(self, area: str) -> str:
        """ウェザーニュースの地域気象解説を取得"""
        return await self.wni.get_area_weather_summary(area)

    async def get_comprehensive_report(self, area: str) -> ComprehensiveWeatherReport:
        """気象庁とウェザーニュースの情報を並行取得し、統合包括レポートを生成する"""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("[tool_registry] Generating comprehensive japan weather report for %s", area)

        # 4つの情報源を並行取得
        f_task = self.jma.get_forecast(area)
        w_task = self.jma.get_warnings(area)
        t_task = self.wni.get_latest_topics(limit=5)
        eq_task = self.jma.get_earthquake_info(limit=3)

        results: list[Any] = list(
            await asyncio.gather(f_task, w_task, t_task, eq_task, return_exceptions=True)
        )

        jma_forecast: JMAForecastResult | None = (
            results[0] if isinstance(results[0], JMAForecastResult) else None
        )
        jma_warnings: JMAWarningResult | None = (
            results[1] if isinstance(results[1], JMAWarningResult) else None
        )
        wni_topics: list[WNITopicItem] = (
            results[2] if isinstance(results[2], list) else []
        )
        earthquakes: list[EarthquakeInfo] = (
            results[3] if isinstance(results[3], list) else []
        )

        return ComprehensiveWeatherReport(
            target_area=area,
            generated_at=now_str,
            jma_forecast=jma_forecast,
            jma_warnings=jma_warnings,
            wni_topics=wni_topics,
            recent_earthquakes=earthquakes,
        )

    async def get_weather_summary(
        self,
        area: str,
        when: str = "tomorrow",
        template: str | None = None,
    ) -> str | None:
        """指定地域の天気予報を取得し、指定日（今日/明日/明後日）の要約テキストを返却する。"""
        log.info("[tool_registry] Getting weather summary for area=%s when=%s", area, when)
        try:
            forecast = await self.get_jma_forecast(area)
            if not forecast or (not forecast.forecasts and not forecast.overview):
                return None
            return forecast.format_summary(when=when, template=template)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("[tool_registry] get_weather_summary network error: area=%r error=%s", area, e)
            return None
        except Exception as e:
            log.warning("[tool_registry] get_weather_summary unexpected error: area=%r error=%s", area, e)
            return None


async def fetch_weather_summary(
    area: str,
    when: str = "tomorrow",
    template: str | None = None,
    service: JapanWeatherService | None = None,
) -> str | None:
    """国内気象情報 MCP 経由で天気予報要約テキストを取得するヘルパー関数"""
    svc = service or JapanWeatherService()
    return await svc.get_weather_summary(area=area, when=when, template=template)

