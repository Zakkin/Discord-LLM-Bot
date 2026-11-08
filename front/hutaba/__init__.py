"""front.hutaba: *.2chan.net（ふたば☆ちゃんねる）操作サブプロジェクト。

- `front.hutaba.client`:        スレッド取得・HTML解析・投稿文整形・MCP/HTTP送信・カタログ取得
- `front.hutaba.monitor`:       自律投稿スケジュール・スレ監視・そうだねフィードバック学習
- `front.hutaba.thread_reader`: スレッド調査結果の整形・直接返信生成
- `front.hutaba.tools`:         カタログ上位/スレッド取得ディスパッチ・ヘルパーローダー
- `front.hutaba.utils`:         URL正規化・ブラウザヘッダー生成
- `front.hutaba.adapter`:       `HutabaAdapter`（`PlatformAdapter` 実装）
"""
from __future__ import annotations

from front.hutaba.adapter import HutabaAdapter
from front.hutaba.client import img2chan as img2chan
from front.hutaba.client import img2channet as img2channet
from front.hutaba.client.img2chan import (
    caption_hutaba_thread_image,
    fetch_img_thread,
    fetch_img_top5_threads,
    format_img_thread_summary,
    format_img_top5_summary,
    is_img_thread_url,
    is_img_top_request,
    submit_img_thread_reply,
)
from front.hutaba.thread_reader import (
    build_img_thread_direct_reply,
    clean_img_thread_fragment,
)
from front.hutaba.utils import (
    _HUTABA_THREAD_URL_PATTERN,
    _IMG_THREAD_URL_PATTERN,
    default_browser_headers,
    extract_hutaba_board_server,
    normalize_hutaba_board_server,
    normalize_img_thread_url,
)

__all__ = [
    "HutabaAdapter",
    "_HUTABA_THREAD_URL_PATTERN",
    "_IMG_THREAD_URL_PATTERN",
    "build_img_thread_direct_reply",
    "caption_hutaba_thread_image",
    "clean_img_thread_fragment",
    "default_browser_headers",
    "extract_hutaba_board_server",
    "fetch_img_thread",
    "fetch_img_top5_threads",
    "format_img_thread_summary",
    "format_img_top5_summary",
    "img2chan",
    "img2channet",
    "is_img_thread_url",
    "is_img_top_request",
    "normalize_hutaba_board_server",
    "normalize_img_thread_url",
    "submit_img_thread_reply",
]
