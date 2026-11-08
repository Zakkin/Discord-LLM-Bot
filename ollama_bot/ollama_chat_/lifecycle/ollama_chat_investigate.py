"""互換シム: このモジュールの実体は `front.discord.lifecycle.ollama_chat_investigate` に移設されました。

旧パス `ollama_bot.ollama_chat_.lifecycle.ollama_chat_investigate` からの import / `unittest.mock.patch` を新モジュールへ透過的に転送します。
新規コードでは `front.discord.lifecycle.ollama_chat_investigate` を直接 import してください。
"""
import sys as _sys

import front.discord.lifecycle.ollama_chat_investigate as _impl
from front.discord.lifecycle.ollama_chat_investigate import *  # noqa: F401,F403
from front.discord.lifecycle.ollama_chat_investigate import (  # noqa: F401
    _normalize_search_query_slang,
    _preserve_factcheck_vital_terms,
    _restore_target_proper_nouns,
)

_sys.modules[__name__] = _impl
