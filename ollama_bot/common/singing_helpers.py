"""歌唱機能に関する入力判定、曲名抽出、歌詞整形、リズム計算ヘルパー。"""
from __future__ import annotations

import re
from typing import NamedTuple

from lib.discord_utils import DISCORD_MENTION_RE as _DISCORD_MENTION_RE, clean_user_input_text
from lib.text_utils import normalize_text

# 中断キーワード
SING_STOP_KEYWORDS = (
    "やめて",
    "止めろ",
    "止めて",
    "やめろ",
    "ストップ",
    "stop",
    "歌うのやめて",
    "歌うの止めて",
    "歌うのやめろ",
    "歌うの止めろ",
    "静かにして",
    "静かに",
    "うるさい",
    "もういい",
    "黙れ",
    "おしまい",
    "終了",
    "終わり",
)

# 歌唱指示の正規表現パターン
# 例:
# 「アイドルを歌って」「怪獣の花唄歌え」「『となりのトトロ』歌ってよ」
# 「新時代を歌うんだ」「lemon歌ってください」
_SING_PATTERNS = [
    # 引用符・カギ括弧で囲まれている場合: 「曲名」歌って, "曲名"を歌え
    re.compile(r"""(?:[「『"“'‘](?P<title_quoted>[^」』"”'’]+)[」』"”'’]\s*(?:の歌|の曲)?\s*(?:を|って)?\s*歌(?:って|え|うんだ|ってください|えよ|おう|うて|ってくれ|えー|ってー))""", re.IGNORECASE),
    # 一般形式: 曲名[を]歌って / 歌え / 歌うんだ
    re.compile(r"""^(?:じゃあ|では|ねえ|ねぇ|おい|さあ)?\s*(?P<title>.+?)(?:の歌|の曲)?\s*(?:を|って)?\s*歌(?:って|え|うんだ|ってください|えよ|おう|うて|ってくれ|えー|ってー)\s*(?:よ|ね|な|ー|！|!|w|ｗ|\?|？)*$""", re.IGNORECASE),
]

# 曲名抽出時の不要なプレフィックス/サフィックス
_STRIP_WORDS = ("ちょっと", "なんか", "適当に", "一曲", "1曲", "さあ", "じゃあ", "では", "ねえ", "ねぇ")


class SingingPhrase(NamedTuple):
    text: str
    delay_sec: float


def normalize_input_text(text: str) -> str:
    """メンションや余分な空白を除去し正規化する（後方互換用）。"""
    return clean_user_input_text(text)



# 歌唱中断指示の正規表現パターン
# 1. 歌唱を明示した停止指示: 「歌うのやめて」「歌うのを止めて」「歌止めて」「歌うのをストップ」など
_SING_STOP_EXPLICIT_RE = re.compile(
    r"^(?:歌(?:う)?(?:こと)?(?:の)?(?:は|を)?)\s*(?:やめて|止め(?:て|ろ)|やめろ|ストップ|stop|終わり|おしまい|終了)(?:して)?(?:[よねなー！!wｗ\?？\s]|くれ|ください|ちょうだい|お願い)*$",
    re.IGNORECASE,
)

# 2. 短い単独の中断・制止・不満の発話:
# 「やめて」「やめろ」「止めろ」「止めて」「ストップ」「stop」「うるさい」「黙れ」「静かに」「静かにして」「もういい」「おしまい」「終了」「終わり」
_SING_STOP_SHORT_RE = re.compile(
    r"^(?:もう\s*)?(?:やめて|止め(?:て|ろ)|やめろ|ストップ|stop|うるさい|黙れ|静かに|もういい|おしまい|終了|終わり)(?:して|してください)?(?:[よねなー！!wｗ\?？\s]|くれ|ください|ちょうだい|にして|にしてよ|にしてくれ)*$",
    re.IGNORECASE,
)


def is_singing_stop_command(text: str) -> bool:
    """ユーザーの発言が歌唱中断指示かどうか判定する。

    ※ 歌詞行（先頭が # や音符）や、歌詞・日常会話文中に含まれる単語（例: 「やめろと言われても」「終わりなき旅」）
       によって誤検知・誤停止しないよう厳格に判定する。
    """
    raw = str(text or "").strip()
    if not raw:
        return False

    # 歌詞フォーマット行（# で始まる行）や歌唱装飾行は絶対に停止コマンドではない
    if raw.startswith("#") or raw.startswith("〜♪") or raw.startswith("♬"):
        return False

    normalized = normalize_input_text(raw)
    if not normalized or normalized.startswith("#"):
        return False

    # 句読点や記号を除去したシンプルテキスト
    cleaned = re.sub(r"[\s!！?？。、.,~〜ー]+", "", normalized)
    if not cleaned:
        return False

    # 正規表現による厳格なパターン判定（元テキストまたは記号除去後テキストのいずれかに合致）
    if _SING_STOP_EXPLICIT_RE.search(normalized) or _SING_STOP_SHORT_RE.search(normalized):
        return True
    if _SING_STOP_EXPLICIT_RE.search(cleaned) or _SING_STOP_SHORT_RE.search(cleaned):
        return True

    return False


def extract_song_title(text: str) -> str | None:
    """メッセージから歌唱リクエストを検知し、曲名を抽出する。"""
    normalized = normalize_input_text(text)
    if not normalized:
        return None

    # 中断コマンドの場合は歌唱リクエストとみなさない
    if is_singing_stop_command(normalized):
        return None

    for pattern in _SING_PATTERNS:
        match = pattern.search(normalized)
        if match:
            title = match.groupdict().get("title_quoted") or match.groupdict().get("title")
            if not title:
                continue
            title = title.strip()
            # カギ括弧やクォートのトリミング
            title = re.sub(r"^[「『\"'“‘\s]+|[」』\"'”’\s]+$", "", title)
            # 前置きの単語をトリミング
            for sw in _STRIP_WORDS:
                if title.startswith(sw):
                    title = title[len(sw):].strip()
            # 末尾の助詞をトリミング
            title = re.sub(r"(?:の歌|の曲)$", "", title).strip()
            if title and len(title) <= 60:
                return title

    return None


import random

# 歌唱用の音符・装飾サフィックス
_SINGING_NOTE_SUFFIXES = ("♪", "♬", "〜♪", "っ♪", "〜♬")


def format_singing_line(line: str) -> str:
    """歌詞の先頭に中見出し (## ) と斜体 (*...*) を付与する。"""
    cleaned = str(line or "").strip()
    if not cleaned:
        return ""

    # 既存の先頭ハッシュ(#)や空白を除去
    cleaned = re.sub(r"^#+\s*", "", cleaned).strip()

    # 既存の斜体マーク (* または _) で囲まれている場合は一旦外して正規化
    if (cleaned.startswith("*") and cleaned.endswith("*") and len(cleaned) >= 2) or (
        cleaned.startswith("_") and cleaned.endswith("_") and len(cleaned) >= 2
    ):
        cleaned = cleaned[1:-1].strip()

    if not cleaned:
        return ""

    return f"## *{cleaned}*"


def compute_phrase_delay(phrase: str, *, min_delay: float = 1.2, max_delay: float = 4.0) -> float:
    """フレーズの文字数、末尾記号（伸び・タメ・アクセント）に応じたダイナミックなリズム待機時間を計算する。"""
    clean = phrase.strip()
    length = len(clean)

    # 1. 文字数に応じた基礎テンポ（短い合いの手は素早く、長いフレーズはゆったり）
    if length <= 5:
        # 短い合いの手・掛け声（例: 「チチをもげ！」「ヘイ！」「おっぱい」）
        base_delay = 1.3 + (length * 0.08)
    elif length <= 12:
        base_delay = 1.8 + ((length - 5) * 0.10)
    elif length <= 22:
        base_delay = 2.4 + ((length - 12) * 0.08)
    else:
        base_delay = 3.2 + ((length - 22) * 0.04)

    # 2. 歌詞のニュアンス・記号に応じたリズム補正
    # タメ・息継ぎ（… や 。）
    if re.search(r"[…\.。]$", clean):
        base_delay += 0.6
    # 伸ばす音（〜 や ー）
    elif re.search(r"[〜~ー]$", clean):
        base_delay += 0.4
    # 歯切れの良いアクセント（！ や ッ や っ）
    elif re.search(r"[!！ッっ]$", clean):
        base_delay -= 0.2

    # 3. 生き生きとした歌唱のための微小なランダム揺らぎ（±0.1秒）
    jitter = random.uniform(-0.1, 0.1)
    total_delay = base_delay + jitter

    return round(min(max(total_delay, min_delay), max_delay), 2)


# ダミー・プレースホルダー行の検出用正規表現パターン
_DUMMY_LINE_PATTERNS = [
    re.compile(r"^(?:行|line|フレーズ|phrase|歌詞)\s*\d+$", re.IGNORECASE),
    re.compile(r"^(?:結果が見つかりませんでした|歌詞が見つかりませんでした|見つかりませんでした|検索結果).*$", re.IGNORECASE),
    re.compile(r"^(?:曲名|タイトル|サビ|1番|2番|歌詞本文|lyrics|title|行ごとの歌詞|歌詞のフレーズ|歌詞リスト|フレーズリスト)$", re.IGNORECASE),
    re.compile(r"^</?(?:think|thought|system|user|assistant)>", re.IGNORECASE),
    re.compile(r"^<[^>]+>$"),
    re.compile(r"^https?://", re.IGNORECASE),
]


def strip_think_tags(text: str) -> str:
    """思考タグ（<think>...</think> や </think> の前方の思考）を確実に除去する。"""
    cleaned = str(text or "")
    if not cleaned:
        return ""

    # </think> がある場合、それより前はすべて思考プロセスなので切り捨てる
    if "</think>" in cleaned.lower():
        parts = re.split(r"</think>", cleaned, flags=re.IGNORECASE)
        cleaned = parts[-1]
    elif "</thought>" in cleaned.lower():
        parts = re.split(r"</thought>", cleaned, flags=re.IGNORECASE)
        cleaned = parts[-1]

    # 単体の <think> や </think> タグを除去
    cleaned = re.sub(r"</?(?:think|thought)>", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def is_dummy_lyrics_line(line: str) -> bool:
    """ダミーやプレースホルダー文字列（行1, line 1, 結果が見つかりませんでした、<think>、行ごとの歌詞等）かどうか判定する。"""
    cleaned = str(line or "").strip()
    if not cleaned:
        return True
    for pat in _DUMMY_LINE_PATTERNS:
        if pat.search(cleaned):
            return True
    # 記号のみの行
    if len(re.sub(r"[\s♪~〜ー\-()（）/／!！?？#＃<>]", "", cleaned)) == 0:
        return True
    return False


def sanitize_lyrics_lines(lines: list[str]) -> list[str]:
    """歌詞行のリストから思考タグ、ダミー行やメタ情報を除去し、正常な歌詞行のみを返す。"""
    cleaned_lines: list[str] = []
    seen: set[str] = set()

    for raw_line in lines:
        stripped = strip_think_tags(raw_line)
        for line in stripped.splitlines():
            raw = str(line or "").strip()
            if not raw or is_dummy_lyrics_line(raw):
                continue
            # メタ情報ヘッダー行を除去
            if re.match(r"^(作詞|作曲|編曲|歌|アーティスト|唄|vocal|lyrics|music|produced|chorus|verse|intro|outro|bridge)[\s:：]", raw, re.IGNORECASE):
                continue
            # 連続重複行をスキップ
            if raw in seen and len(cleaned_lines) > 0 and cleaned_lines[-1] == raw:
                continue
            seen.add(raw)
            cleaned_lines.append(raw)

    return cleaned_lines


def extract_lyrics_from_snippets(snippets_text: str) -> list[str]:
    """検索スニペットから実際の歌詞フレーズ（歌いだし、カギ括弧内の歌詞等）を直接抽出する。"""
    if not snippets_text:
        return []

    extracted: list[str] = []

    # 1. 「歌いだし「...」」や「(歌いだし)...」のパターン
    start_matches = re.findall(r"(?:歌い出し|歌いだし|歌い出|歌い出しは)[\s:：]*[「『]([^」』]+)[」』]", snippets_text)
    for match in start_matches:
        cleaned_match = match.strip()
        if 4 <= len(cleaned_match) <= 40 and not is_dummy_lyrics_line(cleaned_match):
            extracted.append(cleaned_match)
        else:
            for phrase in re.split(r"[、,\n]+", cleaned_match):
                if len(phrase.strip()) >= 3 and not is_dummy_lyrics_line(phrase):
                    extracted.append(phrase.strip())

    # 2. カギ括弧内のセリフ・フレーズ
    quote_matches = re.findall(r"[「『]([^」』]{4,35})[」』]", snippets_text)
    for quote in quote_matches:
        if not re.search(r"(?:歌詞|ページ|サービス|無料|曲名|アルバム|リリース|発売)", quote) and not is_dummy_lyrics_line(quote):
            extracted.append(quote.strip())

    # 3. スニペット内のフレーズ行（句点や読点で区切る）
    for snippet in snippets_text.splitlines():
        clean_snip = snippet.strip()
        if not clean_snip or clean_snip.startswith("【"):
            continue
        # 作詞・作曲などのメタ行を除去
        if re.search(r"(?:作詞|作曲|編曲|挿入歌|主題歌|無料歌詞|音楽情報サイト|一覧で掲載中)", clean_snip):
            after_meta = re.sub(r"^.*?作詞.*?作曲.*?[。、\s]", "", clean_snip)
            after_meta = re.sub(r"^.*?歌い出し.*?[\s:：]", "", after_meta)
            for part in re.split(r"[。！？!?\n]+", after_meta):
                p = part.strip()
                if 4 <= len(p) <= 30 and not is_dummy_lyrics_line(p) and not re.search(r"(?:無料|サービス|サイト|うたてん|歌ネット)", p):
                    extracted.append(p)
        else:
            for part in re.split(r"[。！？!?\n]+", clean_snip):
                p = part.strip()
                if 4 <= len(p) <= 30 and not is_dummy_lyrics_line(p) and not re.search(r"(?:無料|サービス|サイト|うたてん|歌ネット)", p):
                    extracted.append(p)

    return sanitize_lyrics_lines(extracted)


def extract_base_song_title(title: str) -> str:
    """曲名からサブタイトル（〜...〜）、バージョン表記（[TV Size], (feat. ...), -Arrange-等）を除去した本体曲名を取得する。"""
    cleaned = str(title or "").strip()
    if not cleaned:
        return ""
    # チルダ、ハイフン、括弧以降のサブタイトルやバージョン指定を除去
    base = re.sub(r'[\s\u3000]*[〜~～\-－(（【\[「『].*$', '', cleaned).strip()
    return base if base else cleaned


def is_matching_song_title(target_title: str, candidate_title: str) -> bool:
    """検索結果の曲名・タイトルが探している曲と一致しているか検証する。"""
    def _clean(t: str) -> str:
        # 正規化、小文字化、記号除去
        norm = normalize_text(t).lower()
        # カタカナをひらがなに変換
        hiragana = ""
        for c in norm:
            code = ord(c)
            if 0x30A1 <= code <= 0x30F6:
                hiragana += chr(code - 0x60)
            else:
                hiragana += c
        # 記号や空白を除去
        cleaned = re.sub(r"[\s\-_!！?？~〜ー'\"「」『』【】()（）/／:：,、.。]+", "", hiragana)
        return cleaned

    target_clean = _clean(target_title)
    cand_clean = _clean(candidate_title)

    if not target_clean or not cand_clean:
        return False

    # 完全一致、または一方がもう一方を含む（例: 「チチをもげ!」と「パルコ・フォルゴレ チチをもげ!」）
    if target_clean == cand_clean or target_clean in cand_clean:
        return True

    # ターゲットが十分長い場合（3文字以上）、部分一致
    if len(target_clean) >= 3 and cand_clean in target_clean:
        return True

    # サブタイトルやバージョン表記を除去したベース曲名での比較（例: 「童祭 〜for Wedding on avocal」と「童祭」）
    base_target = _clean(extract_base_song_title(target_title))
    if len(base_target) >= 2 and base_target in cand_clean:
        return True

    base_cand = _clean(extract_base_song_title(candidate_title))
    if len(base_cand) >= 2 and base_cand in target_clean:
        return True

    return False


def parse_uta_net_lyrics_page(html: str) -> list[str]:
    """歌ネット（uta-net.com）、うたてん（utaten.com）、J-Lyric等の歌詞ページHTMLから歌詞本文を抽出する。"""
    if not html:
        return []
    from html import unescape

    # 主要歌詞サイトの歌詞コンテナパターン（歌ネット、うたてん、J-Lyric、KKBOX、歌詞ナビ等）
    patterns = [
        r'<div\s+id="kashi_area"[^>]*>(.*?)</div>',
        r'<div\s+itemprop="text"[^>]*>(.*?)</div>',
        r'<p\s+id="Lyric"[^>]*>(.*?)</p>',
        r'<div\s+id="kashibox"[^>]*>(.*?)</div>',
        r'<div\s+class="lyricBody"[^>]*>(.*?)</div>',
        r'<div\s+class="lyric__body"[^>]*>(.*?)</div>',
        r'<div\s+class="styled-lyrics"[^>]*>(.*?)</div>',
        r'<(?:div|p|section)[^>]*(?:id|class)=[\"\'][^\"\']*(?:kashi_area|hiragana|medium|lyricBody|lyric__body|styled-lyrics|lyrics|lyric|song-lyrics)[^\"\']*[\"\'][^>]*>(.*?)</(?:div|p|section)>',
        r'<div\s+class="kashi_area"[^>]*>(.*?)</div>',
        r'<div\s+class="medium"[^>]*>(.*?)</div>',
        r'<div\s+class="hiragana"[^>]*>(.*?)</div>',
        r'<div\s+id="lyrics"[^>]*>(.*?)</div>',
        r'<div\s+class="lyrics"[^>]*>(.*?)</div>',
        r'<div\s+id="kashi"[^>]*>(.*?)</div>',
        r'<div\s+class="kashi"[^>]*>(.*?)</div>',
        r'<div\s+id="lyric"[^>]*>(.*?)</div>',
        r'<div\s+class="lyric"[^>]*>(.*?)</div>',
    ]
    raw_kashi = ""
    for pat in patterns:
        match = re.search(pat, html, flags=re.DOTALL | re.IGNORECASE)
        if match:
            raw_kashi = match.group(1)
            break

    if raw_kashi:
        # ルビの読み仮名タグを除去
        raw_kashi = re.sub(r'<rt>.*?</rt>', '', raw_kashi, flags=re.DOTALL | re.IGNORECASE)
        # <br> を改行に変換
        raw_kashi = re.sub(r'<br\s*/?>', '\n', raw_kashi, flags=re.IGNORECASE)
        # HTMLタグ除去
        raw_kashi = re.sub(r'<[^>]+>', '', raw_kashi)
        lines = [unescape(line).strip() for line in raw_kashi.splitlines() if unescape(line).strip()]
        return sanitize_lyrics_lines(lines)

    return []


def extract_lyrics_from_generic_html(html: str) -> list[str]:
    """Wiki（wikiwiki.jp, atwiki等）、ブログ、ニコニコ大百科等の汎用WebページHTMLから歌詞本文を抽出する。"""
    if not html:
        return []
    from html import unescape

    # 1. 歌詞見出し（「歌詞」「リリック」「Lyrics」等）の直後のセクションを探索
    kashi_section = re.search(
        r'(?:<h2>|<h3>|<h4>|\*+|\b)(?:歌詞|リリック|Lyrics)(?:</h2>|</h3>|</h4>|\*+|\b)(.*?)(?:<h2>|<h3>|<h4>|<hr\s*/?>|<div\s+class="[^\"]*footer|$)',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    target_html = kashi_section.group(1) if kashi_section else html

    # 2. pre や blockquote があれば整形された歌詞テキストの可能性が高い
    blocks = re.findall(r'<(?:pre|blockquote)[^>]*>(.*?)</(?:pre|blockquote)>', target_html, flags=re.DOTALL | re.IGNORECASE)
    if blocks:
        raw_text = "\n".join(blocks)
    else:
        # div#body, div.wiki-content, article, main, entry-content
        body_match = re.search(
            r'<(?:div|article|main|section)[^>]*(?:id|class)=[\"\'][^\"\']*(?:body|content|article|text|entry|main)[^\"\']*[\"\'][^>]*>(.*?)</(?:div|article|main|section)>',
            target_html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        raw_text = body_match.group(1) if body_match else target_html

    # 不要なタグを除去
    raw_text = re.sub(r'<script.*?</script>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)
    raw_text = re.sub(r'<style.*?</style>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)
    raw_text = re.sub(r'<rt>.*?</rt>', '', raw_text, flags=re.DOTALL | re.IGNORECASE)
    raw_text = re.sub(r'</?(?:p|div|li|h\d|tr)[^>]*>', '\n', raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r'<br\s*/?>', '\n', raw_text, flags=re.IGNORECASE)
    raw_text = re.sub(r'<[^>]+>', '', raw_text)

    lines: list[str] = []
    for line in raw_text.splitlines():
        cleaned = unescape(line).strip()
        # 長すぎる散文や見出し記号を除外
        if cleaned and not cleaned.startswith(("*", "#", "-", "=", "※", "//")) and len(cleaned) <= 60:
            lines.append(cleaned)

    return sanitize_lyrics_lines(lines)


def parse_uta_net_search_results(html: str) -> list[tuple[str, str]]:
    """歌ネットの検索結果HTMLから (曲名, 楽曲URL) のリストを抽出する。"""
    if not html:
        return []
    from html import unescape
    matches = re.findall(r'<a[^>]*href="(/song/\d+/?)"[^>]*>(.*?)</a>', html, flags=re.DOTALL | re.IGNORECASE)
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, raw_title in matches:
        title = unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
        full_url = f"https://www.uta-net.com{href}" if href.startswith("/") else href
        if full_url not in seen and title:
            seen.add(full_url)
            results.append((title, full_url))
    return results


def parse_utaten_search_results(html: str) -> list[tuple[str, str]]:
    """うたてんの検索結果HTMLから (曲名, 楽曲URL) のリストを抽出する。"""
    if not html:
        return []
    from html import unescape
    matches = re.findall(r'<a[^>]*href="(/lyric/[^"]+/?)"[^>]*>(.*?)</a>', html, flags=re.DOTALL | re.IGNORECASE)
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, raw_title in matches:
        title = unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
        full_url = f"https://utaten.com{href}" if href.startswith("/") else href
        if full_url not in seen and title:
            seen.add(full_url)
            results.append((title, full_url))
    return results


def parse_jlyric_search_results(html: str) -> list[tuple[str, str]]:
    """J-Lyricの検索結果HTMLから (曲名, 楽曲URL) のリストを抽出する。"""
    if not html:
        return []
    from html import unescape
    matches = re.findall(r'<a[^>]*href="(/artist/[^"]+/[^"]+\.html)"[^>]*>(.*?)</a>', html, flags=re.DOTALL | re.IGNORECASE)
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, raw_title in matches:
        title = unescape(re.sub(r'<[^>]+>', '', raw_title)).strip()
        full_url = f"https://j-lyric.net{href}" if href.startswith("/") else href
        if full_url not in seen and title:
            seen.add(full_url)
            results.append((title, full_url))
    return results


def parse_musescore_tempo(html_or_snippet: str) -> float | None:
    """MuseScoreのページHTMLまたは検索スニペットからBPM/テンポ数値を抽出する。"""
    if not html_or_snippet:
        return None

    # 1. "bpm": 128 または "tempo": 128 または "qpm": 120 (JSONメタデータ)
    bpm_match = re.search(r'"(?:bpm|tempo|qpm)"\s*:\s*(\d{2,3}(?:\.\d+)?)', html_or_snippet, re.IGNORECASE)
    if bpm_match:
        try:
            bpm = float(bpm_match.group(1))
            if 40.0 <= bpm <= 240.0:
                return bpm
        except ValueError:
            pass

    # 2. 120 bpm, BPM: 130, Tempo: 120 等のテキスト表記
    text_match = re.search(r'(?:BPM|bpm|Tempo|tempo|TEMPO)[\s:：=]*(\d{2,3})', html_or_snippet)
    if not text_match:
        text_match = re.search(r'(\d{2,3})\s*(?:BPM|bpm)', html_or_snippet)

    if text_match:
        try:
            bpm = float(text_match.group(1))
            if 40.0 <= bpm <= 240.0:
                return bpm
        except ValueError:
            pass

    return None


def compute_phrase_delay_with_bpm(
    phrase: str,
    bpm: float | None = None,
    *,
    min_delay: float = 1.0,
    max_delay: float = 4.2,
) -> float:
    """BPM（テンポ）を考慮したフレーズの待機時間（秒）を計算する。"""
    if bpm is not None and 40.0 <= bpm <= 240.0:
        # 1拍あたりの秒数 = 60 / bpm
        beat_sec = 60.0 / bpm
        clean = phrase.strip()
        length = len(clean)

        # 文字数に応じて 2拍（半小節）〜4拍（1小節）〜6拍を割り当て
        if length <= 5:
            # 短い合いの手: 2拍（半小節）
            beats = 2.0
        elif length <= 12:
            # 短めフレーズ: 3拍〜4拍
            beats = 3.5
        elif length <= 22:
            # 通常フレーズ: 4拍〜5拍（1小節強）
            beats = 4.5
        else:
            # 長文フレーズ: 6拍（1.5小節）
            beats = 6.0

        # 記号補正
        if re.search(r"[…\.。]$", clean):
            beats += 1.0
        elif re.search(r"[〜~ー]$", clean):
            beats += 0.8
        elif re.search(r"[!！ッっ]$", clean):
            beats -= 0.5

        calc_sec = beats * beat_sec
        jitter = random.uniform(-0.08, 0.08)
        return round(min(max(calc_sec + jitter, min_delay), max_delay), 2)

    # BPM が不明な場合は通常のダイナミックディレイ
    return compute_phrase_delay(phrase, min_delay=min_delay, max_delay=max_delay)


def parse_lyrics_to_phrases(
    raw_lyrics: str,
    *,
    bpm: float | None = None,
    max_phrases: int = 16,
    default_min_delay: float = 1.2,
    default_max_delay: float = 4.0,
) -> list[SingingPhrase]:
    """生歌詞テキストを適切なフレーズ単位に分割し、BPM/リズムに応じたディレイ時間を設定する。"""
    if not raw_lyrics:
        return []

    lines = [line.strip() for line in raw_lyrics.splitlines() if line.strip()]
    lines = sanitize_lyrics_lines(lines)
    phrases: list[SingingPhrase] = []

    for line in lines:
        if len(phrases) >= max_phrases:
            break
        # メタ情報行をスキップ
        if re.match(r"^(作詞|作曲|編曲|歌|アーティスト|唄|vocal|lyrics|music|produced|chorus|verse|intro|outro|bridge)[\s:：]", line, re.IGNORECASE):
            continue

        # 長すぎる行（35文字超）は句読点やスペースで2分割
        if len(line) > 35:
            split_parts = re.split(r"([、, 　])", line)
            chunk = ""
            for part in split_parts:
                if len(chunk) + len(part) <= 30:
                    chunk += part
                else:
                    if chunk.strip():
                        delay = compute_phrase_delay_with_bpm(chunk, bpm=bpm, min_delay=default_min_delay, max_delay=default_max_delay)
                        phrases.append(SingingPhrase(text=chunk.strip(), delay_sec=delay))
                        if len(phrases) >= max_phrases:
                            break
                    chunk = part
            if chunk.strip() and len(phrases) < max_phrases:
                delay = compute_phrase_delay_with_bpm(chunk, bpm=bpm, min_delay=default_min_delay, max_delay=default_max_delay)
        else:
            delay = compute_phrase_delay_with_bpm(line, bpm=bpm, min_delay=default_min_delay, max_delay=default_max_delay)
            phrases.append(SingingPhrase(text=line, delay_sec=delay))

    return phrases


# 歌唱メッセージ判定用パターン
_REACTION_SUFFIX_RE = re.compile(r"\s*\[(?:Bot)?リアクション:[^\]]*\]\s*$")
_SINGING_LYRICS_LINE_RE = re.compile(r"^##\s*[*_].+[*_]$")
_SINGING_START_LINE_RE = re.compile(r"「(?P<title>[^」]+)」.*(?:いくよ〜〜っ|歌詞を思い出すから|歌う準備をするね)")


def is_singing_assistant_line(line: str) -> bool:
    """アシスタントの発言行が歌唱機能（開始合図、歌詞フレーズ、終了合図、中断）によるものか判定する。"""
    raw = str(line or "").strip()
    if not raw:
        return False
    speaker, sep, body = raw.partition(": ")
    if not sep or speaker.strip().lower() not in {"assistant", "ai", "bot", "あなた"}:
        return False
    clean_body = _REACTION_SUFFIX_RE.sub("", body).strip()
    if not clean_body:
        return False
    # 歌詞行
    if _SINGING_LYRICS_LINE_RE.match(clean_body):
        return True
    # 終了合図
    if "おわりっ！聴いてくれてありがと" in clean_body or clean_body.startswith("〜♪（おわりっ！"):
        return True
    # 開始合図
    if _SINGING_START_LINE_RE.search(clean_body) or ("いくよ〜〜っ！♬" in clean_body and "「" in clean_body):
        return True
    # 中断合図
    if clean_body == "歌うのをやめたよ！":
        return True
    return False


def extract_song_title_from_singing_line(line: str) -> str | None:
    """歌唱行（開始行など）から曲名を抽出する。"""
    raw = str(line or "").strip()
    if not raw:
        return None
    _, sep, body = raw.partition(": ")
    clean_body = (_REACTION_SUFFIX_RE.sub("", body) if sep else raw).strip()
    match = _SINGING_START_LINE_RE.search(clean_body)
    if match:
        title = match.group("title").strip()
        if title:
            return title
    m = re.search(r"「(?P<title>[^」]+)」", clean_body)
    if m and ("いくよ" in clean_body or "準備をするね" in clean_body or "歌詞を思い出す" in clean_body):
        title = m.group("title").strip()
        if title:
            return title
    return None


def compact_singing_context_lines(context_lines: list[str]) -> list[str]:
    """会話履歴リスト内の連続する歌唱メッセージ行（歌詞や終了合図）を1行の歌唱事実に集約・置換する。"""
    if not context_lines:
        return []

    result: list[str] = []
    i = 0
    n = len(context_lines)
    last_requested_title: str | None = None

    while i < n:
        line = context_lines[i]
        raw = str(line or "").strip()
        if not raw:
            i += 1
            continue

        # ユーザーの発言から歌唱リクエスト曲名を追跡
        speaker, sep, body = raw.partition(": ")
        if sep and speaker.strip().lower() not in {"assistant", "ai", "bot", "あなた"}:
            clean_user_body = _REACTION_SUFFIX_RE.sub("", body).strip()
            user_title = extract_song_title(clean_user_body)
            if user_title:
                last_requested_title = user_title
            result.append(raw)
            i += 1
            continue

        # アシスタントの歌唱メッセージかどうか判定
        if is_singing_assistant_line(raw):
            # 連続する歌唱行をグループ化
            song_title: str | None = None
            asst_speaker = speaker.strip() or "Assistant"
            while i < n and is_singing_assistant_line(context_lines[i]):
                cur_line = str(context_lines[i] or "").strip()
                t = extract_song_title_from_singing_line(cur_line)
                if t and not song_title:
                    song_title = t
                i += 1

            if not song_title and last_requested_title:
                song_title = last_requested_title

            if song_title:
                summary_line = f"{asst_speaker}: （「{song_title}」を歌った）"
            else:
                summary_line = f"{asst_speaker}: （歌を歌った）"

            result.append(summary_line)
            continue

        result.append(raw)
        i += 1

    return result

