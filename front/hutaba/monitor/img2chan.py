"""img.2chan.netへの自律投稿、スレ監視、そうだね学習を束ねる互換入口。
実装は役割別Mixinへ分割し、このモジュールは既存importとテストパッチの受け口を保ちます。
"""
from __future__ import annotations

import logging
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_float, cfg_int
from ollama_bot.common.json_compat import dumps as json_dumps
from ollama_bot.common.ollama_helpers import call_ollama_json
from front.hutaba.monitor.img2chan_learning import IMG2CHAN_SOUDANE_ANALYSIS_SCHEMA, _Img2chanLearningMixin
from front.hutaba.monitor.img2chan_posting import (
    _Img2chanPostingMixin,
    _is_img2chan_request_thread,
    _is_img2chan_stream_or_live_thread,
)
from front.hutaba.monitor.img2chan_state import _Img2chanStateMixin

log = logging.getLogger("front.hutaba")


def _get_img2chan_helper() -> Any:
    from harness.agent.tool_registry import web_research

    return getattr(web_research, "_img_helper", None)


class _Img2chanEventMixin(_Img2chanPostingMixin, _Img2chanStateMixin, _Img2chanLearningMixin):
    pass
