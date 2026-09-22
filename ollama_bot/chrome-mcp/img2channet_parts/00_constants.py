# Shared constants and schema for IMG thread helpers.
# This part is executed by ../img2channet.py and uses that module's imports.
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("ollama_bot.chrome_mcp.img2channet")

# ================================================================
# IMG スレッド個別閲覧用ヘルパー
# ================================================================

from lib.img2chan_utils import _IMG_THREAD_URL_PATTERN
_IMG_THREAD_POST_CONTEXT_LIMIT = 10
_IMG2CHAN_POST_STATUS_SELECTOR = "#retmestip"
_IMG2CHAN_POST_TEXTAREA_SELECTOR = "#ftxa"
_IMG2CHAN_POST_EMAIL_SELECTOR = "input[name='email']"
_IMG2CHAN_POST_PASSWORD_SELECTOR = "input[name='pwd']"
_IMG2CHAN_POST_SUBMIT_SELECTOR = "#fm input[type='submit']"
_IMG2CHAN_POST_CACHE_TOKEN_URL = "https://img.2chan.net/bin/cachemt7.php"
_IMG2CHAN_POST_MCP_ALLOWED_TOOLS = (
    "chrome_navigate",
    "chrome_get_web_content",
    "chrome_read_page",
    "chrome_close_tabs",
    "chrome_click_element",
    "chrome_fill_or_select",
    "chrome_keyboard",
    "chrome_get_interactive_elements",
)
_IMG2CHAN_POST_MCP_BLOCKED_TOOL_KEYWORDS = (
    "download",
    "upload",
    "delete",
    "remove",
    "purchase",
    "buy",
    "checkout",
    "cart",
    "payment",
)
_IMG_POST_CONTROL_LABELS = (
    "返答前の内省メモ",
    "内省メモ",
    "感情の軸",
    "感情",
    "返答方針",
    "避けること",
    "相手の意図",
    "会話の背景",
    "思考",
    "推論",
    "analysis",
    "reasoning",
)
_IMG_POST_REPLY_LABELS = (
    "投稿本文",
    "書き込み",
    "コメント",
    "comment",
    "reply",
    "answer",
    "final answer",
    "返答",
    "回答",
    "最終返答",
    "最終回答",
    "ユーザー向け返答",
    "ユーザー向け回答",
)

IMG_THREAD_POST_COMMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "comment": {"type": "string"},
        "quote_post_numbers": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        },
        "quote_reply_numbers": {
            "type": "array",
            "items": {"type": "integer"},
            "maxItems": 2,
        },
    },
    "required": ["comment"],
    "additionalProperties": False,
}
