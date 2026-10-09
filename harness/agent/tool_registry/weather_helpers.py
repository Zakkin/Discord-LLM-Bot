# ollama_bot/common/weather_helpers.py
"""国内気象情報連携ヘルパー (ollama_bot/common/weather_helpers.py)

国内気象情報 MCP (harness.agent.tool_registry.mcp.weather: 気象庁 JMA & ウェザーニュース)
をバックエンドとして利用し、Discord 対話における天気質問の判定・地名抽出・予報取得・
フォローアップ応答生成を提供する。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import aiohttp

from lib.discord_utils import strip_discord_mentions
from ollama_bot.common.config_helpers import cfg

log = logging.getLogger("ollama_bot.common.weather_helpers")

# 地名エイリアス辞書（主要都市・都道府県名対応）
_WEATHER_ALIAS: dict[str, str] = {
    # 北海道・東北
    "北海道": "Hokkaido", "道央": "Sapporo", "札幌": "Sapporo", "札幌市": "Sapporo",
    "青森": "Aomori", "青森県": "Aomori", "青森市": "Aomori",
    "岩手": "Iwate", "岩手県": "Iwate", "盛岡": "Morioka", "盛岡市": "Morioka",
    "宮城": "Miyagi", "宮城県": "Miyagi", "仙台": "Sendai", "仙台市": "Sendai",
    "秋田": "Akita", "秋田県": "Akita", "秋田市": "Akita",
    "山形": "Yamagata", "山形県": "Yamagata", "山形市": "Yamagata",
    "福島": "Fukushima", "福島県": "Fukushima", "福島市": "Fukushima",
    # 関東
    "茨城": "Ibaraki", "茨城県": "Ibaraki", "水戸": "Mito", "水戸市": "Mito",
    "栃木": "Tochigi", "栃木県": "Tochigi", "宇都宮": "Utsunomiya", "宇都宮市": "Utsunomiya",
    "群馬": "Gunma", "群馬県": "Gunma", "前橋": "Maebashi", "前橋市": "Maebashi", "高崎": "Takasaki",
    "埼玉": "Saitama", "埼玉県": "Saitama", "さいたま": "Saitama", "さいたま市": "Saitama",
    "千葉": "Chiba", "千葉県": "Chiba", "千葉市": "Chiba",
    "東京": "Tokyo", "東京都": "Tokyo", "都内": "Tokyo",
    "神奈川": "Kanagawa", "神奈川県": "Kanagawa", "横浜": "Yokohama", "横浜市": "Yokohama", "川崎": "Kawasaki",
    # 中部
    "新潟": "Niigata", "新潟県": "Niigata", "新潟市": "Niigata",
    "富山": "Toyama", "富山県": "Toyama", "富山市": "Toyama",
    "石川": "Ishikawa", "石川県": "Ishikawa", "金沢": "Kanazawa", "金沢市": "Kanazawa",
    "福井": "Fukui", "福井県": "Fukui", "福井市": "Fukui",
    "山梨": "Yamanashi", "山梨県": "Yamanashi", "甲府": "Kofu", "甲府市": "Kofu",
    "長野": "Nagano", "長野県": "Nagano", "長野市": "Nagano", "松本": "Matsumoto",
    "岐阜": "Gifu", "岐阜県": "Gifu", "岐阜市": "Gifu",
    "静岡": "Shizuoka", "静岡県": "Shizuoka", "静岡市": "Shizuoka", "浜松": "Hamamatsu",
    "愛知": "Aichi", "愛知県": "Aichi", "名古屋": "Nagoya", "名古屋市": "Nagoya",
    # 近畿
    "三重": "Mie", "三重県": "Mie", "津": "Tsu", "津市": "Tsu",
    "滋賀": "Shiga", "滋賀県": "Shiga", "大津": "Otsu", "大津市": "Otsu",
    "京都": "Kyoto", "京都府": "Kyoto", "京都市": "Kyoto",
    "大阪": "Osaka", "大阪府": "Osaka", "大阪市": "Osaka", "堺": "Sakai",
    "兵庫": "Hyogo", "兵庫県": "Hyogo", "神戸": "Kobe", "神戸市": "Kobe",
    "奈良": "Nara", "奈良県": "Nara", "奈良市": "Nara",
    "和歌山": "Wakayama", "和歌山県": "Wakayama", "和歌山市": "Wakayama",
    # 中国
    "鳥取": "Tottori", "鳥取県": "Tottori", "鳥取市": "Tottori",
    "島根": "Shimane", "島根県": "Shimane", "松江": "Matsue", "松江市": "Matsue",
    "岡山": "Okayama", "岡山県": "Okayama", "岡山市": "Okayama",
    "広島": "Hiroshima", "広島県": "Hiroshima", "広島市": "Hiroshima",
    "山口": "Yamaguchi", "山口県": "Yamaguchi", "山口市": "Yamaguchi",
    # 四国
    "徳島": "Tokushima", "徳島県": "Tokushima", "徳島市": "Tokushima",
    "香川": "Kagawa", "香川県": "Kagawa", "高松": "Takamatsu", "高松市": "Takamatsu",
    "愛媛": "Ehime", "愛媛県": "Ehime", "松山": "Matsuyama", "松山市": "Matsuyama",
    "高知": "Kochi", "高知県": "Kochi", "高知市": "Kochi",
    # 九州・沖縄
    "福岡": "Fukuoka", "福岡県": "Fukuoka", "福岡市": "Fukuoka", "博多": "Fukuoka", "北九州": "Kitakyushu",
    "佐賀": "Saga", "佐賀県": "Saga", "佐賀市": "Saga",
    "長崎": "Nagasaki", "長崎県": "Nagasaki", "長崎市": "Nagasaki",
    "熊本": "Kumamoto", "熊本県": "Kumamoto", "熊本市": "Kumamoto",
    "大分": "Oita", "大分県": "Oita", "大分市": "Oita",
    "宮崎": "Miyazaki", "宮崎県": "Miyazaki", "宮崎市": "Miyazaki",
    "鹿児島": "Kagoshima", "鹿児島県": "Kagoshima", "鹿児島市": "Kagoshima",
    "沖縄": "Okinawa", "沖縄県": "Okinawa", "那覇": "Naha", "那覇市": "Naha",
    # 海外
    "アメリカ": "United States", "米国": "United States",
    "ニューヨーク": "New York", "ロサンゼルス": "Los Angeles",
    "ロンドン": "London", "パリ": "Paris",
}


def _normalize_weather_query_text(text: str) -> str:
    """メッセージテキストからDiscordメンションや不要な接続詞を除去して正規化する"""
    t = strip_discord_mentions(text)
    # 先頭の呼びかけ・間投詞・接続詞を除去（地名「奈良」との誤爆を防ぐため「なら」は単独で含まない）
    t = re.sub(r"^\s*(じゃあ|では|それじゃあ|それなら|あと|ちなみに|おい|ねえ|なあ|おーい|もしもし|ねぇ|あの|あのー)[、,\s]*", "", t)
    # 余分な空白を圧縮
    t = re.sub(r"\s+", " ", t).strip()
    return t


def looks_like_weather_query(text: str) -> bool:
    """天気や気温に関する問い合わせかどうかを判定する"""
    t = _normalize_weather_query_text(text)
    weather_words = ("天気", "気温", "雨", "晴れ", "曇り", "降水", "雪", "傘")
    return any(w in t for w in weather_words)


def is_explicit_weather_request(text: str) -> bool:
    """明示的な天気予報要求かどうかを判定する"""
    t = _normalize_weather_query_text(text)
    if not t:
        return False
    explicit_patterns = [
        r".+の天気(?:を教えろ|を教えて|教えて|は？|は\?|は)$",
        r".+の気温(?:を教えろ|を教えて|教えて|は？|は\?|は)$",
        r"(?:今日|明日|明後日|きのう|昨日)の.+(?:って|は)?(?:どんな)?天気",
        r"(?:今日|明日|明後日|きのう|昨日)の.+(?:って|は)?(?:どんな)?気温",
        r"^(?:今日|明日|明後日|きのう|昨日)の天気(?:を教えろ|を教えて|教えて|は？|は\?|は)$",
        r"^(?:今日|明日|明後日|きのう|昨日)の気温(?:を教えろ|を教えて|教えて|は？|は\?|は)$",
    ]
    if any(re.search(p, t) for p in explicit_patterns):
        return True
    return bool(re.search(r"(教えろ|教えて|予報|forecast)", t))


def normalize_place_name(place: str) -> str:
    """地名の表記ゆれを吸収する"""
    p = (place or "").strip()
    return _WEATHER_ALIAS.get(p, p)


def _clean_place_candidate(place: str) -> str:
    """抽出された地名候補から接続詞や助詞、日付表現を安全にトリミングする"""
    p = (place or "").strip()
    # 先頭の接続詞・呼びかけ・間投詞
    p = re.sub(r"^(?:じゃあ|では|それじゃあ|それなら|あと|ちなみに|おい|ねえ|なあ|おーい|もしもし)[、,\s]*", "", p)
    # 先頭・末尾の日付表現
    p = re.sub(r"^(?:今日|明日|明後日|きのう|昨日)[の\s]*", "", p)
    p = re.sub(r"[の\s]*(?:今日|明日|明後日|きのう|昨日)[の\s]*$", "", p)
    # 末尾の助詞（の、は、って、で、を、が）
    p = re.sub(r"(?:の|は|って|で|を|が)$", "", p).strip()
    # 先頭末尾の記号・空白
    p = re.sub(r"^[、,\s]+", "", p).strip()
    p = re.sub(r"[、,\s]+$", "", p).strip()
    return p


def extract_weather_place(text: str) -> Optional[str]:
    """テキストから天気対象の地名を抽出する"""
    t = _normalize_weather_query_text(text)
    if not t:
        return None

    # 先頭の日付指定を剥がした文字列も用意
    t_no_date = re.sub(r"^(?:今日|明日|明後日|きのう|昨日)[の\s]*", "", t)

    patterns = [
        r"^(.+?)の天気(?:を教えろ|を教えて|教えて|は？|は\?|は)?$",
        r"^(.+?)の気温(?:を教えろ|を教えて|教えて|は？|は\?|は)?$",
        r"^(.+?)(?:って|は)?(?:どんな)?天気",
        r"^(.+?)(?:って|は)?(?:どんな)?気温",
        r"(.+?)の天気(?:を教えろ|を教えて|教えて|は？|は\?|は)?$",
        r"(.+?)の気温(?:を教えろ|を教えて|教えて|は？|は\?|は)?$",
    ]

    for target in (t_no_date, t):
        for p in patterns:
            m = re.search(p, target)
            if m:
                place = _clean_place_candidate(m.group(1))
                if place:
                    return normalize_place_name(place)

        m = re.search(r"(.+?)(?:の)?(?:天気|気温)", target)
        if m:
            place = _clean_place_candidate(m.group(1))
            if place:
                return normalize_place_name(place)

    return None


async def extract_weather_place_from_context(context_lines: list[str]) -> Optional[str]:
    """会話履歴から過去に言及された地名を抽出する"""
    for line in reversed(context_lines[-5:]):
        _, _, msg = line.partition(":")
        place = extract_weather_place(msg.strip())
        if place:
            return place
    return None


def format_weather_reply(
    place_name: str,
    when_label: str,
    weather_text: str,
    max_temp: Optional[float | str],
    min_temp: Optional[float | str],
    rain_prob: Optional[float | str],
) -> str:
    """天気予報の返信文テンプレートを適用してテキストを生成する"""
    template = cfg(
        "OLLAMA_WEATHER_REPLY_TEMPLATE",
        "{place}の{when}の天気は{weather}です。最高{max}℃、最低{min}℃、降水確率は最大{rain}%です。"
    )

    def _fmt(v: Optional[float | str]) -> str:
        if v is None:
            return "不明"
        s = str(v).replace("℃", "").replace("%", "").strip()
        return s if s else "不明"

    return template.format(
        place=place_name,
        when=when_label,
        weather=weather_text,
        max=_fmt(max_temp),
        min=_fmt(min_temp),
        rain=_fmt(rain_prob),
    )


async def fetch_weather_summary(place_name: str, when: str = "tomorrow") -> Optional[str]:
    """国内気象情報 MCP (harness.agent.tool_registry.mcp.weather) 経由で天気予報を取得する。"""
    if not place_name:
        return None
    template = cfg("OLLAMA_WEATHER_REPLY_TEMPLATE", None)
    log.info("[weather_helpers] Fetching weather summary via MCP for place=%r when=%r", place_name, when)
    try:
        from harness.agent.tool_registry.mcp.weather import JapanWeatherService
        service = JapanWeatherService()
        return await service.get_weather_summary(area=place_name, when=when, template=template)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("fetch_weather_summary network error: place=%r error=%s", place_name, e)
        return None
    except Exception as e:
        log.warning("fetch_weather_summary unexpected error: place=%r error=%s", place_name, e)
        return None


def extract_weather_fact_from_text(text: str) -> Optional[dict[str, object]]:
    """Botの過去発言から場所・日時・天気・気温・降水確率の事実を抽出する"""
    t = (text or "").strip()
    if not t:
        return None

    # 例: "東京の明日の天気は晴れです。最高25℃、最低18℃、降水確率は最大10%です。"
    m = re.search(
        r"(?P<place>.+?)の(?P<when>今日|明日|明後日)(?:の天気)?は(?P<weather>.+?)(?:だな…|です。|だ。|だよ。|だろ。)",
        t,
    )
    if not m:
        return None

    def _to_float(v: Optional[str]) -> Optional[float]:
        if not v:
            return None
        try:
            return float(v)
        except Exception:
            return None

    max_m = re.search(r"最高(?P<max>-?\d+(?:\.\d+)?)℃", t)
    min_m = re.search(r"最低(?P<min>-?\d+(?:\.\d+)?)℃", t)
    rain_m = re.search(r"降水確率は最大(?P<rain>-?\d+(?:\.\d+)?)%", t)

    return {
        "place": m.group("place").strip(),
        "when": m.group("when").strip(),
        "weather": m.group("weather").strip(),
        "max": _to_float(max_m.group("max") if max_m else None),
        "min": _to_float(min_m.group("min") if min_m else None),
        "rain": _to_float(rain_m.group("rain") if rain_m else None),
    }


async def extract_recent_weather_fact_from_context(context_lines: list[str]) -> Optional[dict[str, object]]:
    """会話履歴から直近の天気予報の事実を抽出する"""
    for line in reversed(context_lines[-8:]):
        _, _, msg = line.partition(":")
        fact = extract_weather_fact_from_text(msg.strip())
        if fact:
            return fact
    return None


def _format_weather_followup_template(key: str, default: str, **values: object) -> str:
    template = str(cfg(key, default) or default)
    try:
        return template.format(**values)
    except Exception:
        return default.format(**values)


def build_weather_followup_reply(user_text: str, fact: dict[str, object]) -> str:
    """直前の天気情報に対する後続の短文質問（雨降る？最高気温は？など）に回答する"""
    t = _normalize_weather_query_text(user_text)
    weather = str(fact.get("weather") or "")
    rain = fact.get("rain")
    max_temp = fact.get("max")
    min_temp = fact.get("min")

    rain_words = ("雨", "小雨", "霧雨", "にわか雨", "雷雨", "雪")
    rainy = any(w in weather for w in rain_words)
    if isinstance(rain, (int, float)) and rain >= 30:
        rainy = True

    if re.search(r"(雨|降水|降る|傘)", t):
        if rainy:
            if isinstance(rain, (int, float)):
                return _format_weather_followup_template(
                    "OLLAMA_WEATHER_FOLLOWUP_RAINY_WITH_PROB_TEMPLATE",
                    "そうだな。降る可能性はある。少なくとも{weather}予報で、降水確率は最大{rain}%くらいだ。",
                    weather=weather,
                    rain=rain,
                )
            return _format_weather_followup_template(
                "OLLAMA_WEATHER_FOLLOWUP_RAINY_TEMPLATE",
                "そうだな。少なくとも{weather}予報だから、雨は考えておいたほうがいい。",
                weather=weather,
            )
        return _format_weather_followup_template(
            "OLLAMA_WEATHER_FOLLOWUP_NOT_RAINY_TEMPLATE",
            "いや、雨は強くなさそうだな。天気としては{weather}寄りだ。",
            weather=weather,
        )

    if re.search(r"(最高|暑|気温)", t):
        if isinstance(max_temp, (int, float)):
            return _format_weather_followup_template(
                "OLLAMA_WEATHER_FOLLOWUP_MAX_TEMP_TEMPLATE",
                "最高気温は{max}℃くらいだ。",
                max=max_temp,
            )
    if re.search(r"(最低|寒)", t):
        if isinstance(min_temp, (int, float)):
            return _format_weather_followup_template(
                "OLLAMA_WEATHER_FOLLOWUP_MIN_TEMP_TEMPLATE",
                "最低気温は{min}℃くらいだ。",
                min=min_temp,
            )
    if re.search(r"(気温)", t):
        if isinstance(max_temp, (int, float)) and isinstance(min_temp, (int, float)):
            return _format_weather_followup_template(
                "OLLAMA_WEATHER_FOLLOWUP_TEMP_RANGE_TEMPLATE",
                "気温は最高{max}℃、最低{min}℃くらいだな。",
                max=max_temp,
                min=min_temp,
            )

    return _format_weather_followup_template(
        "OLLAMA_WEATHER_FOLLOWUP_DEFAULT_TEMPLATE",
        "そうだな。{when}は{weather}予報だ。",
        when=fact.get("when") or "その日",
        weather=weather,
    )
