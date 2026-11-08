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

from front.hutaba.utils import _HUTABA_THREAD_URL_PATTERN, _IMG_THREAD_URL_PATTERN
_IMG_THREAD_POST_CONTEXT_LIMIT = 10
_IMG2CHAN_POST_STATUS_SELECTOR = "#retmestip"
_IMG2CHAN_POST_TEXTAREA_SELECTOR = "#ftxa"
_IMG2CHAN_POST_EMAIL_SELECTOR = "input[name='email']"
_IMG2CHAN_POST_PASSWORD_SELECTOR = "input[name='pwd']"
_IMG2CHAN_POST_SUBMIT_SELECTOR = "#fm input[type='submit']"
_IMG2CHAN_POST_CACHE_TOKEN_URL = "https://img.2chan.net/bin/cachemt7.php"


def _get_hutaba_cache_token_url(server: str = "img") -> str:
    srv = "may" if str(server or "").lower() == "may" else "img"
    return f"https://{srv}.2chan.net/bin/cachemt7.php"

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
        "thought": {
            "type": "string",
            "description": (
                "返信を生成する前の3段階推論メモ（本文には出力しないこと）。"
                "【Step A: 暗黙の文脈・先行話題の特定】対象レスが反応している直前の話題・先行レス（何についての議論・幻滅・ツッコミか）を特定する。"
                "【Step B: 主張の検証とクリティカルな別視点・カウンターの検討】相手の主張を額面通り鵜呑みにせず、『本当にそうか？（美化されていないか？裏の実態・狂人・泥沼はどうだったか？）』と疑い、安易な全肯定・懐古ポエムを避けて『実態のシニカルな補足』『裏の真実の提示』『鋭いツッコミ』のカウンター方針を決定する（皮肉・大喜利フリの場合は尊大な代弁ボケまたは定型句突き放しを選択）。※重要: [対象レスへの他人のツッコミ]や直近レスで既に他人が言った回答・数字・意見を語尾だけ変えて繰り返したり複数繋ぎ合わせるキメラ合体は厳禁。既出のツッコミは『使用禁止の切り口』として除外し、必ず別の視点・背景理由を立てること。"
                "【Step C: 表現のサニタイズ】文頭での相手単語のオウム返し（『〇〇…！』等）、抽象ポエム（『あの頃の熱さ〜』等）、他人の既存レスの言い換えパクり、他レスとのキメラ合成を排除し、スレの空気感に踏み込んだ短い一言を設計する。"
            ),
        },
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
        "comment": {"type": "string"},
    },
    "required": ["thought", "quote_post_numbers", "comment"],
    "additionalProperties": False,
}
