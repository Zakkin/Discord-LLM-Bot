"""後方互換性のためのollama_chat_textsアグリゲーター。
実装は harness.agent.prompt_parser.prompt_manager に集約・移行しました。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import harness
except ModuleNotFoundError:
    _repo_root = Path(__file__).resolve().parents[2]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))

from harness.agent.prompt_parser.prompt_manager.analysis_prompts import *  # noqa: F401, F403
from harness.agent.prompt_parser.prompt_manager.system_prompts import *  # noqa: F401, F403
from harness.agent.prompt_parser.prompt_manager.retry_prompts import *  # noqa: F401, F403
from harness.agent.prompt_parser.prompt_manager.retry_prompts import _slim_base_prompt_for_retry  # noqa: F401
