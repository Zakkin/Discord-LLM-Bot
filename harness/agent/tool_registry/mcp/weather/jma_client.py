"""気象庁 (JMA: Japan Meteorological Agency) データ取得クライアント

公式サイト: https://www.jma.go.jp/jma/index.html
防災情報公開APIから天気予報、天気概況、警報・注意報、地震情報を取得・パースする。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp

from .types import (
    DailyForecast,
    EarthquakeInfo,
    JMAForecastResult,
    JMAWarningResult,
    clean_jma_weather_text,
    resolve_jma_area_code,
)

log = logging.getLogger("harness.agent.tool_registry.mcp.weather.jma")

JMA_FORECAST_BASE_URL = "https://www.jma.go.jp/bosai/forecast/data/forecast"
JMA_OVERVIEW_BASE_URL = "https://www.jma.go.jp/bosai/forecast/data/overview_forecast"
JMA_WARNING_BASE_URL = "https://www.jma.go.jp/bosai/warning/data/warning"
JMA_QUAKE_URL = "https://www.jma.go.jp/bosai/quake/data/list.json"

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; DiscordAIBot/1.0; +https://www.jma.go.jp)"

# 気象庁 警報・注意報コード辞書
JMA_WARNING_CODES: dict[str, tuple[str, str]] = {
    "02": ("暴風雪警報", "warning"),
    "03": ("大雨警報", "warning"),
    "04": ("洪水警報", "warning"),
    "05": ("暴風警報", "warning"),
    "06": ("大雪警報", "warning"),
    "07": ("波浪警報", "warning"),
    "08": ("高潮警報", "warning"),
    "10": ("大雨注意報", "advisory"),
    "12": ("大雪注意報", "advisory"),
    "13": ("風雪注意報", "advisory"),
    "14": ("雷注意報", "advisory"),
    "15": ("強風注意報", "advisory"),
    "16": ("波浪注意報", "advisory"),
    "17": ("融雪注意報", "advisory"),
    "18": ("洪水注意報", "advisory"),
    "19": ("高潮注意報", "advisory"),
    "20": ("濃霧注意報", "advisory"),
    "21": ("乾燥注意報", "advisory"),
    "22": ("なだれ注意報", "advisory"),
    "23": ("低温注意報", "advisory"),
    "24": ("霜注意報", "advisory"),
    "25": ("着氷注意報", "advisory"),
    "26": ("着雪注意報", "advisory"),
    "33": ("大雨特別警報", "warning"),
    "35": ("暴風特別警報", "warning"),
}


class JMAClient:
    """気象庁 (JMA) 公開データ連携クライアント"""

    def __init__(self, timeout_sec: float = 12.0) -> None:
        self.timeout_sec = timeout_sec

    async def _fetch_json(self, url: str) -> Any:
        """指定URLからJSONデータを安全に非同期取得する"""
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    log.warning("[tool_registry] JMA fetch failed url=%s status=%d", url, resp.status)
                    return None
                return await resp.json(content_type=None)

    async def get_forecast(self, area: str) -> JMAForecastResult:
        """指定地域の天気予報および天気概況を取得する"""
        area_code = resolve_jma_area_code(area)
        forecast_url = f"{JMA_FORECAST_BASE_URL}/{area_code}.json"
        overview_url = f"{JMA_OVERVIEW_BASE_URL}/{area_code}.json"

        log.info("[tool_registry] Fetching JMA forecast for area=%s (code=%s)", area, area_code)

        gathered_results: list[Any] = list(
            await asyncio.gather(
                self._fetch_json(forecast_url),
                self._fetch_json(overview_url),
                return_exceptions=True,
            )
        )
        forecast_data: Any = gathered_results[0] if len(gathered_results) > 0 else None
        overview_data: Any = gathered_results[1] if len(gathered_results) > 1 else None

        overview_text = ""
        office_name = "気象庁"
        report_time = ""

        if isinstance(overview_data, dict):
            overview_text = str(overview_data.get("text") or "").strip()
            office_name = str(overview_data.get("publishingOffice") or office_name)
            report_time = str(overview_data.get("reportDatetime") or "")

        daily_forecasts: list[DailyForecast] = []
        if isinstance(forecast_data, list) and len(forecast_data) > 0:
            first_block = forecast_data[0]
            if isinstance(first_block, dict):
                office_name = str(first_block.get("publishingOffice") or office_name)
                report_time = report_time or str(first_block.get("reportDatetime") or "")
                time_series = first_block.get("timeSeries") or []

                if isinstance(time_series, list) and len(time_series) > 0:
                    # Series 0: 天気・風・波
                    weathers_data = time_series[0]
                    time_defines = weathers_data.get("timeDefines", [])
                    areas = weathers_data.get("areas", [])
                    target_area_data = areas[0] if areas else {}

                    weathers = target_area_data.get("weathers", [])
                    winds = target_area_data.get("winds", [])
                    waves = target_area_data.get("waves", [])

                    # Series 1: 降水確率
                    pops_list: list[str] = []
                    if len(time_series) > 1:
                        pop_areas = time_series[1].get("areas", [])
                        if pop_areas:
                            pops_list = pop_areas[0].get("pops", [])

                    # Series 2: 気温 (最低 / 最高)
                    temps_list: list[str] = []
                    temp_time_defines: list[str] = []
                    if len(time_series) > 2:
                        temp_time_defines = time_series[2].get("timeDefines", [])
                        temp_areas = time_series[2].get("areas", [])
                        if temp_areas:
                            temps_list = temp_areas[0].get("temps", [])

                    for idx, t_def in enumerate(time_defines):
                        date_str = str(t_def)[:10] if len(str(t_def)) >= 10 else str(t_def)
                        raw_weather = weathers[idx] if idx < len(weathers) else "不明"
                        weather_desc = clean_jma_weather_text(raw_weather)
                        pop_desc = f"{pops_list[idx]}%" if idx < len(pops_list) and pops_list[idx] else ""
                        wind_desc = winds[idx] if idx < len(winds) else ""
                        wave_desc = waves[idx] if idx < len(waves) else ""

                        # 気温の割り当て
                        t_min = ""
                        t_max = ""

                        # 1. temp_time_defines の日付と一致する気温を抽出
                        matched_temps: list[str] = []
                        if temp_time_defines and len(temp_time_defines) == len(temps_list):
                            for td_str, val in zip(temp_time_defines, temps_list):
                                if str(td_str)[:10] == date_str and str(val).strip():
                                    matched_temps.append(str(val).strip())

                        if len(matched_temps) == 2:
                            t_min = f"{matched_temps[0]}℃"
                            t_max = f"{matched_temps[1]}℃"
                        elif len(matched_temps) == 1:
                            t_max = f"{matched_temps[0]}℃"
                        else:
                            # 2. フォールバック: 発表時刻・インデックスに応じた判定
                            if len(temps_list) == 2:
                                # 17時発表: [明日の最低, 明日の最高]
                                if idx == 1 or len(time_defines) == 1:
                                    t_min = f"{temps_list[0]}℃"
                                    t_max = f"{temps_list[1]}℃"
                                elif idx == 0:
                                    t_max = f"{temps_list[1]}℃"
                            elif len(temps_list) == 3:
                                # 11時発表: [今日の最高, 明日の最低, 明日の最高]
                                if idx == 0:
                                    t_max = f"{temps_list[0]}℃"
                                elif idx == 1:
                                    t_min = f"{temps_list[1]}℃"
                                    t_max = f"{temps_list[2]}℃"
                            elif len(temps_list) >= 4:
                                # 05時発表: [今日の最低, 今日の最高, 明日の最低, 明日の最高]
                                if idx == 0:
                                    t_min = f"{temps_list[0]}℃"
                                    t_max = f"{temps_list[1]}℃"
                                elif idx == 1:
                                    t_min = f"{temps_list[2]}℃"
                                    t_max = f"{temps_list[3]}℃"

                        daily_forecasts.append(
                            DailyForecast(
                                date=date_str,
                                weather=weather_desc,
                                pop=pop_desc,
                                temp_min=t_min,
                                temp_max=t_max,
                                wind=wind_desc,
                                wave=wave_desc,
                            )
                        )

        # フォールバック（データ取得不可時の安全なレスポンス）
        if not daily_forecasts and not overview_text:
            overview_text = f"気象庁から {area} (コード {area_code}) の天気予報データを取得できませんでした。ネットワーク状況または地域名をご確認ください。"

        return JMAForecastResult(
            area_name=area,
            area_code=area_code,
            publishing_office=office_name,
            report_datetime=report_time,
            overview=overview_text,
            forecasts=daily_forecasts,
            raw_data=forecast_data if isinstance(forecast_data, dict) else None,
        )

    async def get_warnings(self, area: str) -> JMAWarningResult:
        """指定地域の発表中気象警報・注意報を取得する"""
        area_code = resolve_jma_area_code(area)
        warning_url = f"{JMA_WARNING_BASE_URL}/{area_code}.json"

        log.info("[tool_registry] Fetching JMA warnings for area=%s (code=%s)", area, area_code)

        try:
            data = await self._fetch_json(warning_url)
        except Exception as e:
            log.warning("[tool_registry] JMA warnings exception: %s", e)
            data = None

        if not isinstance(data, dict):
            return JMAWarningResult(
                area_name=area,
                area_code=area_code,
                report_datetime="",
                warnings=[],
                advisories=[],
                is_clear=True,
            )

        report_datetime = str(data.get("reportDatetime") or "")
        area_types = data.get("areaTypes") or []
        found_warnings: set[str] = set()
        found_advisories: set[str] = set()

        if isinstance(area_types, list):
            for a_type in area_types:
                areas = a_type.get("areas") or [] if isinstance(a_type, dict) else []
                for a_item in areas:
                    warnings = a_item.get("warnings") or [] if isinstance(a_item, dict) else []
                    for w in warnings:
                        if isinstance(w, dict):
                            code = str(w.get("code") or "")
                            status = str(w.get("status") or "")
                            if status in ("発表", "継続", "") and code in JMA_WARNING_CODES:
                                name, level = JMA_WARNING_CODES[code]
                                if level == "warning":
                                    found_warnings.add(name)
                                else:
                                    found_advisories.add(name)

        return JMAWarningResult(
            area_name=area,
            area_code=area_code,
            report_datetime=report_datetime,
            warnings=sorted(list(found_warnings)),
            advisories=sorted(list(found_advisories)),
            is_clear=len(found_warnings) == 0 and len(found_advisories) == 0,
        )

    async def get_earthquake_info(self, limit: int = 3) -> list[EarthquakeInfo]:
        """最新の地震情報を取得する"""
        log.info("[tool_registry] Fetching JMA earthquake list limit=%d", limit)
        try:
            data = await self._fetch_json(JMA_QUAKE_URL)
        except Exception as e:
            log.warning("[tool_registry] JMA earthquake exception: %s", e)
            data = None

        results: list[EarthquakeInfo] = []
        if isinstance(data, list):
            for item in data[: max(1, limit)]:
                if not isinstance(item, dict):
                    continue
                dt = str(item.get("rdt") or item.get("at") or "")
                anm = str(item.get("anm") or "不明")
                mag = str(item.get("mag") or "-")
                maxi = str(item.get("maxi") or "-")
                dep = str(item.get("dep") or "-")
                tsunami = str(item.get("tsunami") or "なし")

                # 震度コードの簡易変換
                intensity_map = {
                    "1": "震度1", "2": "震度2", "3": "震度3", "4": "震度4",
                    "5-": "震度5弱", "5+": "震度5強", "6-": "震度6弱", "6+": "震度6強", "7": "震度7"
                }
                max_int_text = intensity_map.get(maxi, f"震度{maxi}" if maxi != "-" else "不明")

                results.append(
                    EarthquakeInfo(
                        datetime=dt,
                        epicenter=anm,
                        max_intensity=max_int_text,
                        magnitude=mag,
                        depth=f"{dep}km" if dep.isdigit() else dep,
                        tsunami_info=tsunami,
                    )
                )

        return results
