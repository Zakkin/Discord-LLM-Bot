# ollama_bot/common/weather_helpers.py
import asyncio
import logging
import re
from typing import Optional
from urllib.parse import quote

import aiohttp

from lib.discord_utils import strip_discord_mentions
from .config_helpers import cfg
from .http_session import get_shared_http_session
from .json_compat import loads as json_loads

log = logging.getLogger("ollama_bot.common.weather_helpers")

_WEATHER_ALIAS = {
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
    "アメリカ": "United States", "米国": "United States", "アメリカ合衆国": "United States",
    "ニューヨーク": "New York", "ロサンゼルス": "Los Angeles",
    "北朝鮮": "Pyongyang", "韓国": "Seoul", "ソウル": "Seoul",
    "中国": "Beijing", "北京": "Beijing", "上海": "Shanghai",
    "台湾": "Taipei", "台北": "Taipei",
    "ロシア": "Moscow", "モスクワ": "Moscow",
    "イギリス": "London", "英国": "London", "ロンドン": "London",
    "フランス": "Paris", "パリ": "Paris",
    "ドイツ": "Berlin", "ベルリン": "Berlin",
}

_PRESET_COORDINATES: dict[str, tuple[float, float, str]] = {
    # 日本の主要都市・県庁所在地
    "Tokyo": (35.6895, 139.6917, "Tokyo (Tokyo)"),
    "Sapporo": (43.0642, 141.3469, "Sapporo (Hokkaido)"),
    "Hokkaido": (43.0642, 141.3469, "Sapporo (Hokkaido)"),
    "Aomori": (40.8244, 140.7400, "Aomori (Aomori)"),
    "Iwate": (39.7036, 141.1527, "Morioka (Iwate)"),
    "Morioka": (39.7036, 141.1527, "Morioka (Iwate)"),
    "Miyagi": (38.2682, 140.8694, "Sendai (Miyagi)"),
    "Sendai": (38.2682, 140.8694, "Sendai (Miyagi)"),
    "Akita": (39.7186, 140.1024, "Akita (Akita)"),
    "Yamagata": (38.2404, 140.3633, "Yamagata (Yamagata)"),
    "Fukushima": (37.7500, 140.4678, "Fukushima (Fukushima)"),
    "Ibaraki": (36.3418, 140.4468, "Mito (Ibaraki)"),
    "Mito": (36.3418, 140.4468, "Mito (Ibaraki)"),
    "Tochigi": (36.5657, 139.8836, "Utsunomiya (Tochigi)"),
    "Utsunomiya": (36.5657, 139.8836, "Utsunomiya (Tochigi)"),
    "Gunma": (36.3911, 139.0608, "Maebashi (Gunma)"),
    "Maebashi": (36.3911, 139.0608, "Maebashi (Gunma)"),
    "Takasaki": (36.3219, 139.0033, "Takasaki (Gunma)"),
    "Saitama": (35.8617, 139.6455, "Saitama (Saitama)"),
    "Chiba": (35.6073, 140.1063, "Chiba (Chiba)"),
    "Kanagawa": (35.4478, 139.6425, "Yokohama (Kanagawa)"),
    "Yokohama": (35.4478, 139.6425, "Yokohama (Kanagawa)"),
    "Kawasaki": (35.5309, 139.7031, "Kawasaki (Kanagawa)"),
    "Niigata": (37.9022, 139.0236, "Niigata (Niigata)"),
    "Toyama": (36.6953, 137.2113, "Toyama (Toyama)"),
    "Ishikawa": (36.5947, 136.6256, "Kanazawa (Ishikawa)"),
    "Kanazawa": (36.5947, 136.6256, "Kanazawa (Ishikawa)"),
    "Fukui": (36.0652, 136.2216, "Fukui (Fukui)"),
    "Yamanashi": (35.6639, 138.5683, "Kofu (Yamanashi)"),
    "Kofu": (35.6639, 138.5683, "Kofu (Yamanashi)"),
    "Nagano": (36.6513, 138.1810, "Nagano (Nagano)"),
    "Matsumoto": (36.2381, 137.9720, "Matsumoto (Nagano)"),
    "Gifu": (35.3912, 136.7223, "Gifu (Gifu)"),
    "Shizuoka": (34.9756, 138.3828, "Shizuoka (Shizuoka)"),
    "Hamamatsu": (34.7108, 137.7261, "Hamamatsu (Shizuoka)"),
    "Aichi": (35.1815, 136.9064, "Nagoya (Aichi)"),
    "Nagoya": (35.1815, 136.9064, "Nagoya (Aichi)"),
    "Mie": (34.7303, 136.5086, "Tsu (Mie)"),
    "Tsu": (34.7303, 136.5086, "Tsu (Mie)"),
    "Shiga": (35.0045, 135.8686, "Otsu (Shiga)"),
    "Otsu": (35.0045, 135.8686, "Otsu (Shiga)"),
    "Kyoto": (35.0116, 135.7681, "Kyoto (Kyoto)"),
    "Osaka": (34.6937, 135.5023, "Osaka (Osaka)"),
    "Sakai": (34.5733, 135.4830, "Sakai (Osaka)"),
    "Hyogo": (34.6901, 135.1955, "Kobe (Hyogo)"),
    "Kobe": (34.6901, 135.1955, "Kobe (Hyogo)"),
    "Nara": (34.6851, 135.8049, "Nara (Nara)"),
    "Wakayama": (34.2260, 135.1675, "Wakayama (Wakayama)"),
    "Tottori": (35.5011, 134.2351, "Tottori (Tottori)"),
    "Shimane": (35.4723, 133.0505, "Matsue (Shimane)"),
    "Matsue": (35.4723, 133.0505, "Matsue (Shimane)"),
    "Okayama": (34.6618, 133.9344, "Okayama (Okayama)"),
    "Hiroshima": (34.3963, 132.4594, "Hiroshima (Hiroshima)"),
    "Yamaguchi": (34.1859, 131.4706, "Yamaguchi (Yamaguchi)"),
    "Tokushima": (34.0658, 134.5593, "Tokushima (Tokushima)"),
    "Takamatsu": (34.3401, 134.0433, "Takamatsu (Kagawa)"),
    "Kagawa": (34.3401, 134.0433, "Takamatsu (Kagawa)"),
    "Ehime": (33.8417, 132.7657, "Matsuyama (Ehime)"),
    "Matsuyama": (33.8417, 132.7657, "Matsuyama (Ehime)"),
    "Kochi": (33.5597, 133.5311, "Kochi (Kochi)"),
    "Fukuoka": (33.5904, 130.4017, "Fukuoka (Fukuoka)"),
    "Kitakyushu": (33.8833, 130.8752, "Kitakyushu (Fukuoka)"),
    "Saga": (33.2494, 130.2988, "Saga (Saga)"),
    "Nagasaki": (32.7448, 129.8737, "Nagasaki (Nagasaki)"),
    "Kumamoto": (32.7898, 130.7417, "Kumamoto (Kumamoto)"),
    "Oita": (33.2382, 131.6126, "Oita (Oita)"),
    "Miyazaki": (31.9111, 131.4239, "Miyazaki (Miyazaki)"),
    "Kagoshima": (31.5602, 130.5581, "Kagoshima (Kagoshima)"),
    "Okinawa": (26.2124, 127.6809, "Naha (Okinawa)"),
    "Naha": (26.2124, 127.6809, "Naha (Okinawa)"),
    # 海外
    "United States": (38.8951, -77.0364, "Washington, D.C. (United States)"),
    "New York": (40.7128, -74.0060, "New York (United States)"),
    "Los Angeles": (34.0522, -118.2437, "Los Angeles (United States)"),
    "Pyongyang": (39.0392, 125.7625, "Pyongyang (North Korea)"),
    "Seoul": (37.5665, 126.9780, "Seoul (South Korea)"),
    "Beijing": (39.9042, 116.4074, "Beijing (China)"),
    "Shanghai": (31.2304, 121.4737, "Shanghai (China)"),
    "Taipei": (25.0330, 121.5654, "Taipei (Taiwan)"),
    "Moscow": (55.7558, 37.6173, "Moscow (Russia)"),
    "London": (51.5074, -0.1278, "London (United Kingdom)"),
    "Paris": (48.8566, 2.3522, "Paris (France)"),
    "Berlin": (52.5200, 13.4050, "Berlin (Germany)"),
}


def _normalize_weather_query_text(text: str) -> str:
    t = strip_discord_mentions(text)

    # 先頭の呼びかけ・間投詞・接続詞を除去（地名「奈良」との誤爆を防ぐため「なら」は単独で含まない）
    t = re.sub(r"^\s*(じゃあ|では|それじゃあ|それなら|あと|ちなみに|おい|ねえ|なあ|おーい|もしもし|ねぇ|あの|あのー)[、,\s]*", "", t)

    # 余分な空白を圧縮
    t = re.sub(r"\s+", " ", t).strip()
    return t



def looks_like_weather_query(text: str) -> bool:
    t = _normalize_weather_query_text(text)
    weather_words = ("天気", "気温", "雨", "晴れ", "曇り", "降水", "雪", "傘")
    return any(w in t for w in weather_words)



def is_explicit_weather_request(text: str) -> bool:
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
    p = (place or "").strip()
    return _WEATHER_ALIAS.get(p, p)


def _clean_place_candidate(place: str) -> str:
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
    for line in reversed(context_lines[-5:]):
        _, _, msg = line.partition(":")
        place = extract_weather_place(msg.strip())
        if place:
            return place
    return None


async def geocode_place(place_name: str) -> Optional[tuple[float, float, str]]:
    query = normalize_place_name(place_name)
    if query in _PRESET_COORDINATES:
        return _PRESET_COORDINATES[query]
    if place_name in _PRESET_COORDINATES:
        return _PRESET_COORDINATES[place_name]

    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={quote(query)}&count=5&language=ja&format=json"
    )

    try:
        session = await get_shared_http_session(timeout_sec=15.0)
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("geocode_place bad status: place=%r status=%d", place_name, resp.status)
                return None
            data = await resp.json(loads=json_loads)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("geocode_place network error: place=%r query=%r error=%s", place_name, query, e)
        return None
    except Exception as e:
        log.warning("geocode_place unexpected error: place=%r query=%r error=%s", place_name, query, e)
        return None

    results = data.get("results") or []
    if not results:
        return None

    first = results[0]
    lat = first.get("latitude")
    lon = first.get("longitude")
    name = first.get("name") or place_name
    country = first.get("country")
    admin1 = first.get("admin1")

    if lat is None or lon is None:
        return None

    resolved_name = str(name)
    if admin1 and admin1 != name:
        resolved_name = f"{name} ({admin1})"
    elif country and country != name:
        resolved_name = f"{name} ({country})"

    return float(lat), float(lon), resolved_name


def weather_code_to_japanese(code: int) -> str:
    mapping = {
        0: "快晴",
        1: "おおむね晴れ",
        2: "晴れ時々くもり",
        3: "くもり",
        45: "霧",
        48: "霧",
        51: "小雨",
        53: "雨",
        55: "強い雨",
        61: "小雨",
        63: "雨",
        65: "強い雨",
        71: "小雪",
        73: "雪",
        75: "大雪",
        80: "にわか雨",
        81: "強めのにわか雨",
        82: "激しいにわか雨",
        95: "雷雨",
    }
    return mapping.get(code, "不明")


def format_weather_reply(
    place_name: str,
    when_label: str,
    weather_text: str,
    max_temp: Optional[float],
    min_temp: Optional[float],
    rain_prob: Optional[float],
) -> str:
    template = cfg(
        "OLLAMA_WEATHER_REPLY_TEMPLATE",
        "{place}の{when}の天気は{weather}です。最高{max}℃、最低{min}℃、降水確率は最大{rain}%です。"
    )

    def _fmt_temp(v: Optional[float]) -> str:
        return "不明" if v is None else str(v)

    def _fmt_rain(v: Optional[float]) -> str:
        return "不明" if v is None else str(v)

    return template.format(
        place=place_name,
        when=when_label,
        weather=weather_text,
        max=_fmt_temp(max_temp),
        min=_fmt_temp(min_temp),
        rain=_fmt_rain(rain_prob),
    )


def _format_weather_followup_template(key: str, default: str, **values: object) -> str:
    template = str(cfg(key, default) or default)
    try:
        return template.format(**values)
    except Exception:
        return default.format(**values)


async def fetch_weather_summary(place_name: str, when: str = "tomorrow") -> Optional[str]:
    geo = await geocode_place(place_name)
    if not geo:
        return None

    lat, lon, resolved_name = geo

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=Asia%2FTokyo&forecast_days=3"
    )

    try:
        session = await get_shared_http_session(timeout_sec=15.0)
        async with session.get(url) as resp:
            if resp.status != 200:
                log.warning("fetch_weather_summary bad status: place=%r status=%d", place_name, resp.status)
                return None
            data = await resp.json(loads=json_loads)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("fetch_weather_summary network error: place=%r error=%s", place_name, e)
        return None
    except Exception as e:
        log.warning("fetch_weather_summary unexpected error: place=%r error=%s", place_name, e)
        return None

    daily = data.get("daily") or {}
    times = daily.get("time") or []
    weather_codes = daily.get("weather_code") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    pop = daily.get("precipitation_probability_max") or []

    if not times:
        return None

    if when == "day_after_tomorrow":
        idx = 2 if len(times) > 2 else (1 if len(times) > 1 else 0)
        when_label = "明後日"
    elif when == "tomorrow":
        idx = 1 if len(times) > 1 else 0
        when_label = "明日"
    else:
        idx = 0
        when_label = "今日"

    code = int(weather_codes[idx]) if idx < len(weather_codes) else -1
    max_temp = tmax[idx] if idx < len(tmax) else None
    min_temp = tmin[idx] if idx < len(tmin) else None
    rain_prob = pop[idx] if idx < len(pop) else None

    jp_weather = weather_code_to_japanese(code)

    return format_weather_reply(
        resolved_name,
        when_label,
        jp_weather,
        max_temp,
        min_temp,
        rain_prob,
    )


def extract_weather_fact_from_text(text: str) -> Optional[dict[str, object]]:
    t = (text or "").strip()
    if not t:
        return None

    m = re.search(
        r"(?P<place>.+?)の(?P<when>今日|明日)は(?P<weather>.+?)(?:だな…|です。|だ。|だよ。|だろ。)"
        r"最高(?P<max>-?\d+(?:\.\d+)?)℃、最低(?P<min>-?\d+(?:\.\d+)?)℃、降水確率は最大(?P<rain>-?\d+(?:\.\d+)?)%",
        t,
    )
    if not m:
        return None

    def _to_float(v: str) -> Optional[float]:
        try:
            return float(v)
        except Exception:
            return None

    return {
        "place": m.group("place").strip(),
        "when": m.group("when").strip(),
        "weather": m.group("weather").strip(),
        "max": _to_float(m.group("max")),
        "min": _to_float(m.group("min")),
        "rain": _to_float(m.group("rain")),
    }


async def extract_recent_weather_fact_from_context(context_lines: list[str]) -> Optional[dict[str, object]]:
    for line in reversed(context_lines[-8:]):
        _, _, msg = line.partition(":")
        fact = extract_weather_fact_from_text(msg.strip())
        if fact:
            return fact
    return None


def build_weather_followup_reply(user_text: str, fact: dict[str, object]) -> str:
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
