"""
自律エージェント行動、内省、内部欲求更新、定期発話を担当するMixin。

【概要】
ファイル肥大化防止および保守性向上のため、実際の定義は `agent/` フォルダ内のモジュール群に分割されました。
このファイルは後方互換性のために、分割されたモジュールからすべての定義をインポートし、
再エクスポートするアグリゲーターとしての役割を持ちます。
"""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from ..common.config_helpers import *
from ..common.context_helpers import *
from ..common.discord_helpers import *
from ..common.emotion_helpers import *
from ..common.ollama_helpers import *
from ..common.reflection_logic import *
from ..common.reply_helpers import *
from .agent import *
from .agent.core_mixin import OllamaChatAgentMixin
from .agent.loops import _AgentLoopsMixin
from .agent.monologue import (
    _AGENT_MONOLOGUE_SCHEMA,
    _AGENT_TOPIC_SCHEMA,
    _CHAT_SPECIAL_TOKEN_RE,
    _AgentMonologueMixin,
    _clean_agent_monologue,
    _clean_agent_topic,
    _normalize_agent_topic_text,
    _topic_compare_key,
    _topic_matches_memory,
)
from .agent.proactive import _AgentProactiveMixin
from .agent.reflection import _AgentReflectionMixin
from .ollama_chat_helpers import *

__all__ = [
    "OllamaChatAgentMixin",
    "_AgentMonologueMixin",
    "_AgentProactiveMixin",
    "_AgentReflectionMixin",
    "_AgentLoopsMixin",
    "_AGENT_TOPIC_SCHEMA",
    "_AGENT_MONOLOGUE_SCHEMA",
    "_CHAT_SPECIAL_TOKEN_RE",
    "_normalize_agent_topic_text",
    "_topic_compare_key",
    "_topic_matches_memory",
    "_clean_agent_topic",
    "_clean_agent_monologue",
]
