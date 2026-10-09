"""国内気象情報 MCP データモデル・型定義 (mcp/weather/types.py)

気象庁 (JMA) および ウェザーニュース (Weathernews) から取得する
気象データ、警報・注意報、トピックス、地震情報のデータクラスを定義する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any


# 気象庁 (JMA) 都道府県・主要都市からエリアコード(6桁)へのマッピング
JMA_AREA_CODE_MAP: dict[str, str] = {
    # 北海道地方
    "北海道": "016000",
    "道央": "016000",
    "札幌": "016000",
    "札幌市": "016000",
    "函館": "017000",
    "旭川": "012000",
    "釧路": "014100",
    # 東北地方
    "青森": "020000",
    "青森県": "020000",
    "岩手": "030000",
    "岩手県": "030000",
    "盛岡": "030000",
    "宮城": "040000",
    "宮城県": "040000",
    "仙台": "040000",
    "仙台市": "040000",
    "秋田": "050000",
    "秋田県": "050000",
    "山形": "060000",
    "山形県": "060000",
    "福島": "070000",
    "福島県": "070000",
    # 関東甲信地方
    "茨城": "080000",
    "茨城県": "080000",
    "水戸": "080000",
    "栃木": "090000",
    "栃木県": "090000",
    "宇都宮": "090000",
    "群馬": "100000",
    "群馬県": "100000",
    "前橋": "100000",
    "埼玉": "110000",
    "埼玉県": "110000",
    "さいたま": "110000",
    "さいたま市": "110000",
    "大宮": "110000",
    "大宮区": "110000",
    "浦和": "110000",
    "川越": "110000",
    "所沢": "110000",
    "熊谷": "110000",
    "千葉": "120000",
    "千葉県": "120000",
    "船橋": "120000",
    "柏": "120000",
    "東京": "130000",
    "東京都": "130000",
    "都内": "130000",
    "八王子": "130000",
    "八王子市": "130000",
    "立川": "130000",
    "町田": "130000",
    "神奈川": "140000",
    "神奈川県": "140000",
    "横浜": "140000",
    "横浜市": "140000",
    "川崎": "140000",
    "川崎市": "140000",
    "相模原": "140000",
    "山梨": "190000",
    "山梨県": "190000",
    "甲府": "190000",
    "長野": "200000",
    "長野県": "200000",
    # 北陸・東海地方
    "新潟": "150000",
    "新潟県": "150000",
    "富山": "160000",
    "富山県": "160000",
    "石川": "170000",
    "石川県": "170000",
    "金沢": "170000",
    "福井": "180000",
    "福井県": "180000",
    "岐阜": "210000",
    "岐阜県": "210000",
    "静岡": "220000",
    "静岡県": "220000",
    "愛知": "230000",
    "愛知県": "230000",
    "名古屋": "230000",
    "名古屋市": "230000",
    "三重": "240000",
    "三重県": "240000",
    # 近畿地方
    "滋賀": "250000",
    "滋賀県": "250000",
    "京都": "260000",
    "京都府": "260000",
    "京都市": "260000",
    "大阪": "270000",
    "大阪府": "270000",
    "大阪市": "270000",
    "兵庫": "280000",
    "兵庫県": "280000",
    "神戸": "280000",
    "神戸市": "280000",
    "奈良": "290000",
    "奈良県": "290000",
    "和歌山": "300000",
    "和歌山県": "300000",
    # 中国地方
    "鳥取": "310000",
    "鳥取県": "310000",
    "島根": "320000",
    "島根県": "320000",
    "松江": "320000",
    "岡山": "330000",
    "岡山県": "330000",
    "広島": "340000",
    "広島県": "340000",
    "山口": "350000",
    "山口県": "350000",
    # 四国地方
    "徳島": "360000",
    "徳島県": "360000",
    "香川": "370000",
    "香川県": "370000",
    "高松": "370000",
    "愛媛": "380000",
    "愛媛県": "380000",
    "松山": "380000",
    "高知": "390000",
    "高知県": "390000",
    # 九州・沖縄地方
    "福岡": "400000",
    "福岡県": "400000",
    "福岡市": "400000",
    "博多": "400000",
    "佐賀": "410000",
    "佐賀県": "410000",
    "長崎": "420000",
    "長崎県": "420000",
    "熊本": "430000",
    "熊本県": "430000",
    "大分": "440000",
    "大分県": "440000",
    "宮崎": "450000",
    "宮崎県": "450000",
    "鹿児島": "460100",
    "鹿児島県": "460100",
    "沖縄": "471000",
    "沖縄県": "471000",
    "那覇": "471000",
    "那覇市": "471000",
    # 英語表記
    "hokkaido": "016000", "sapporo": "016000",
    "aomori": "020000", "iwate": "030000", "morioka": "030000",
    "miyagi": "040000", "sendai": "040000", "akita": "050000",
    "yamagata": "060000", "fukushima": "070000",
    "ibaraki": "080000", "mito": "080000", "tochigi": "090000",
    "utsunomiya": "090000", "gunma": "100000", "maebashi": "100000",
    "takasaki": "100000", "saitama": "110000", "chiba": "120000",
    "tokyo": "130000", "kanagawa": "140000", "yokohama": "140000",
    "niigata": "150000", "toyama": "160000", "ishikawa": "170000",
    "kanazawa": "170000", "fukui": "180000", "yamanashi": "190000",
    "kofu": "190000", "nagano": "200000", "matsumoto": "200000",
    "gifu": "210000", "shizuoka": "220000", "hamamatsu": "220000",
    "aichi": "230000", "nagoya": "230000", "mie": "240000",
    "tsu": "240000", "shiga": "250000", "otsu": "250000",
    "kyoto": "260000", "osaka": "270000", "hyogo": "280000",
    "kobe": "280000", "nara": "290000", "wakayama": "300000",
    "tottori": "310000", "shimane": "320000", "matsue": "320000",
    "okayama": "330000", "hiroshima": "340000", "yamaguchi": "350000",
    "tokushima": "360000", "kagawa": "370000", "takamatsu": "370000",
    "ehime": "380000", "matsuyama": "380000", "kochi": "390000",
    "fukuoka": "400000", "kitakyushu": "400000", "saga": "410000",
    "nagasaki": "420000", "kumamoto": "430000", "oita": "440000",
    "miyazaki": "450000", "kagoshima": "460100", "okinawa": "471000",
    "naha": "471000",
}


def resolve_jma_area_code(area_name: str) -> str:
    """地域名（都道府県名・都市名）から気象庁エリアコード(6桁)を解決する。未解決時は東京(130000)をデフォルトとする。"""
    cleaned = area_name.strip()
    if cleaned in JMA_AREA_CODE_MAP:
        return JMA_AREA_CODE_MAP[cleaned]
    lower = cleaned.lower()
    if lower in JMA_AREA_CODE_MAP:
        return JMA_AREA_CODE_MAP[lower]
    # 接尾辞を除去して再検索
    for suffix in ("都", "府", "県", "市", "区"):
        if cleaned.endswith(suffix):
            cand = cleaned[: -len(suffix)]
            if cand in JMA_AREA_CODE_MAP:
                return JMA_AREA_CODE_MAP[cand]
    # 部分一致
    for name, code in JMA_AREA_CODE_MAP.items():
        if name in cleaned or cleaned in name or name in lower:
            return code
    return "130000"  # デフォルト: 東京


def clean_jma_weather_text(weather: str) -> str:
    """気象庁の天気予報文に含まれる不自然な全角スペース・連続空白を自然な日本語表現に整形する。

    例:
        'くもり　所により　昼前　から　夕方　雨' -> 'くもり、所により昼前から夕方雨'
        '晴れ　時々　くもり' -> '晴れ時々くもり'
        '雨　のち　晴れ' -> '雨のち晴れ'
        'くもり　一時　雨' -> 'くもり一時雨'
    """
    if not weather:
        return ""
    # 全角スペースを半角スペースに統一しトリム
    t = str(weather).replace("\u3000", " ").strip()
    t = re.sub(r" +", " ", t)

    # 助詞・接続語の前後の空白を結合（"昼前 から 夕方" -> "昼前から夕方" 等）
    t = re.sub(r"\s*(時々|のち|一時|から|まで)\s*", r"\1", t)

    # "所により" の前に読点を付与し、前後の空白を詰める
    t = re.sub(r"\s*所により\s*", "、所により", t)

    # 語尾の「雨」「雪」「雷雨」「みぞれ」「霧」の前の空白を詰める
    t = re.sub(r"\s+(雨|雪|雷雨|みぞれ|霧)", r"\1", t)

    # 日本語文字（全角・非ASCII）同士の間の不要スペースをすべて除去
    t = re.sub(r"([^\x00-\x7F])\s+([^\x00-\x7F])", r"\1\2", t)

    return t.strip(" 、")


@dataclass
class DailyForecast:
    """1日分の天気予報データ"""
    date: str  # 例: "2026-10-04"
    weather: str  # 例: "晴れ 時々 曇り"
    pop: str = ""  # 降水確率 (例: "20%")
    temp_min: str = ""  # 最低気温 (例: "15℃")
    temp_max: str = ""  # 最高気温 (例: "24℃")
    wind: str = ""  # 風
    wave: str = ""  # 波


@dataclass
class JMAForecastResult:
    """気象庁 天気予報結果モデル"""
    area_name: str
    area_code: str
    publishing_office: str
    report_datetime: str
    overview: str = ""
    forecasts: list[DailyForecast] = field(default_factory=list)
    raw_data: dict[str, Any] | None = None

    def to_formatted_text(self) -> str:
        """LLMおよびユーザー向けの要約テキストを生成"""
        lines = [
            f"【気象庁公式 天気予報】 {self.area_name} ({self.publishing_office})",
            f"発表時刻: {self.report_datetime}",
        ]
        if self.overview:
            lines.append(f"\n■ 天気概況:\n{self.overview.strip()}")

        if self.forecasts:
            lines.append("\n■ 日別予報:")
            for fc in self.forecasts:
                parts = [f"- {fc.date}: {fc.weather}"]
                if fc.temp_max or fc.temp_min:
                    temp_str = f"気温(最高/最低): {fc.temp_max or '-'} / {fc.temp_min or '-'}"
                    parts.append(temp_str)
                if fc.pop:
                    parts.append(f"降水確率: {fc.pop}")
                if fc.wind:
                    parts.append(f"風: {fc.wind}")
                lines.append(" | ".join(parts))
        return "\n".join(lines)

    def get_forecast_for_when(self, when: str = "tomorrow") -> DailyForecast | None:
        """指定された対象日（今日/明日/明後日）に対応する DailyForecast を取得する"""
        if not self.forecasts:
            return None
        w = when.lower().strip()
        if w in ("day_after_tomorrow", "明後日", "あさって", "2"):
            idx = 2 if len(self.forecasts) > 2 else (1 if len(self.forecasts) > 1 else 0)
        elif w in ("tomorrow", "明日", "あした", "1"):
            idx = 1 if len(self.forecasts) > 1 else 0
        else:
            idx = 0
        return self.forecasts[idx] if idx < len(self.forecasts) else self.forecasts[0]

    def format_summary(self, when: str = "tomorrow", template: str | None = None) -> str:
        """指定された対象日（今日/明日/明後日）の天気要約テキストを生成する"""
        w = when.lower().strip()
        when_labels = {
            "day_after_tomorrow": "明後日",
            "明後日": "明後日",
            "あさって": "明後日",
            "tomorrow": "明日",
            "明日": "明日",
            "あした": "明日",
            "today": "今日",
            "今日": "今日",
            "きょう": "今日",
        }
        when_label = when_labels.get(w, "明日" if "明日" in w or "tomorrow" in w else "今日")
        fc = self.get_forecast_for_when(when)

        if not fc:
            if self.overview:
                return f"{self.area_name}の天気概況: {self.overview.strip()}"
            return f"{self.area_name}の天気予報データはありません。"

        # 天気テキストの空白サニタイズ
        weather_text = clean_jma_weather_text(fc.weather)

        # 気温や降水確率の抽出
        max_str = fc.temp_max.replace("℃", "").strip() if fc.temp_max else "不明"
        min_str = fc.temp_min.replace("℃", "").strip() if fc.temp_min else "不明"
        pop_str = fc.pop.replace("%", "").strip() if fc.pop else "不明"

        if template:
            try:
                return template.format(
                    place=self.area_name,
                    when=when_label,
                    weather=weather_text,
                    max=max_str,
                    min=min_str,
                    rain=pop_str,
                )
            except Exception:
                pass

        if max_str != "不明" or min_str != "不明":
            return (
                f"{self.area_name}の{when_label}の天気は{weather_text}です。"
                f"最高{max_str}℃、最低{min_str}℃、降水確率は最大{pop_str}%です。"
            )
        else:
            return (
                f"{self.area_name}の{when_label}の天気は{weather_text}です。"
                f"降水確率は最大{pop_str}%です。"
            )


@dataclass
class JMAWarningResult:
    """気象庁 気象警報・注意報モデル"""
    area_name: str
    area_code: str
    report_datetime: str
    warnings: list[str] = field(default_factory=list)  # 警報リスト
    advisories: list[str] = field(default_factory=list)  # 注意報リスト
    is_clear: bool = False  # 発表なしの場合True

    def to_formatted_text(self) -> str:
        """警報・注意報の要約テキスト"""
        lines = [
            f"【気象庁公式 気象警報・注意報】 {self.area_name}",
            f"発表時刻: {self.report_datetime or '最新'}",
        ]
        if self.is_clear:
            lines.append("現在、発表されている警報・注意報はありません。")
            return "\n".join(lines)

        if self.warnings:
            lines.append(f"🚨 【警報】: {', '.join(self.warnings)}")
        if self.advisories:
            lines.append(f"⚠️ 【注意報】: {', '.join(self.advisories)}")
        if not self.warnings and not self.advisories:
            lines.append("現在、発表されている警報・注意報はありません。")
        return "\n".join(lines)


@dataclass
class EarthquakeInfo:
    """地震情報モデル"""
    datetime: str
    epicenter: str  # 震源地
    max_intensity: str  # 最大震度
    magnitude: str  # マグニチュード
    depth: str  # 深さ
    tsunami_info: str  # 津波情報

    def to_formatted_text(self) -> str:
        return (
            f"発生日時: {self.datetime} | 震源地: {self.epicenter} | "
            f"最大震度: {self.max_intensity} | M: {self.magnitude} | "
            f"深さ: {self.depth} | 津波: {self.tsunami_info}"
        )


@dataclass
class WNITopicItem:
    """ウェザーニュース トピックス記事モデル"""
    title: str
    summary: str = ""
    published_at: str = ""
    url: str = ""

    def to_formatted_text(self) -> str:
        parts = [f"・【{self.title}】"]
        if self.summary:
            parts.append(f"  {self.summary}")
        if self.published_at:
            parts.append(f"  ({self.published_at})")
        if self.url:
            parts.append(f"  URL: {self.url}")
        return "\n".join(parts)


@dataclass
class ComprehensiveWeatherReport:
    """気象庁とウェザーニュースを統合した包括気象レポート"""
    target_area: str
    generated_at: str
    jma_forecast: JMAForecastResult | None = None
    jma_warnings: JMAWarningResult | None = None
    wni_topics: list[WNITopicItem] = field(default_factory=list)
    recent_earthquakes: list[EarthquakeInfo] = field(default_factory=list)

    def to_formatted_text(self) -> str:
        sections: list[str] = [
            f"══════════════════════════════════════════════════════",
            f"🗾 【国内気象総合レポート】 対象地域: {self.target_area}",
            f"取得日時: {self.generated_at}",
            f"情報ソース: 気象庁 (https://www.jma.go.jp) / ウェザーニュース (https://weathernews.jp)",
            f"══════════════════════════════════════════════════════",
        ]

        # 1. 警報・注意報
        if self.jma_warnings:
            sections.append("\n" + self.jma_warnings.to_formatted_text())

        # 2. 気象庁天気予報
        if self.jma_forecast:
            sections.append("\n" + self.jma_forecast.to_formatted_text())

        # 3. ウェザーニュース最新トピックス
        if self.wni_topics:
            sections.append("\n■ 【ウェザーニュース 最新トピックス・実況解説】")
            for topic in self.wni_topics[:5]:
                sections.append(topic.to_formatted_text())

        # 4. 直近地震情報
        if self.recent_earthquakes:
            sections.append("\n■ 【気象庁 最新地震情報 (直近)】")
            for eq in self.recent_earthquakes[:3]:
                sections.append(f"- {eq.to_formatted_text()}")

        sections.append("══════════════════════════════════════════════════════")
        return "\n".join(sections)
