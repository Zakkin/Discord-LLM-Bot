from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from lib.config_utils import get_bot_config_name, resolve_isolated_path
from harness.agent.memory_manager import MemoryStore
from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_int
from harness.agent.tool_registry.game_pool_manager import GamePoolManager
from harness.agent.prompt_parser.prompt_manager.thread_tracker import ThreadTracker
from ollama_bot.ollama_chat_ import emotion
from ollama_bot.ollama_chat_.ollama_chat_types import EmotionState, MessageRuntime, UserRelationship
from front.discord.commands import umigame as ollama_chat_umigame
from front.discord.commands import twenty_doors as ollama_chat_20doors
from harness.agent.orchestrator import AgentOrchestrator
from front.discord.commands import sing as ollama_chat_sing
from front.discord.commands import image_search as ollama_chat_image_search
from front.discord.lifecycle import ollama_chat_lifecycle_base
from front.discord.lifecycle import ollama_chat_investigate
from front.discord.lifecycle import ollama_chat_model_call
from front.discord.reply import reply as ollama_chat_reply
from ollama_bot.ollama_chat_ import ollama_chat_agent
from front.discord import events as ollama_chat_events
from front.discord import admin as ollama_chat_admin


class OllamaChatCog(
    ollama_chat_admin.OllamaChatAdminMixin,
    emotion.OllamaChatEmotionMixin,
    ollama_chat_umigame.OllamaChatUmigameMixin,
    ollama_chat_20doors.OllamaChat20DoorsMixin,
    ollama_chat_sing.OllamaChatSingMixin,
    ollama_chat_image_search.OllamaChatImageSearchMixin,
    ollama_chat_lifecycle_base.OllamaChatLifecycleBaseMixin,
    ollama_chat_investigate.OllamaChatInvestigateMixin,
    ollama_chat_model_call.OllamaChatModelCallMixin,
    ollama_chat_reply.OllamaChatReplyMixin,
    ollama_chat_agent.OllamaChatAgentMixin,
    ollama_chat_events.OllamaChatEventMixin,
    commands.Cog,
):
    @property
    def is_active(self) -> bool:
        return getattr(self, "_bot_active", True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.orchestrator = AgentOrchestrator()
        self._bot_active: bool = cfg_bool("BOT_ACTIVE_ON_STARTUP", True)
        self.semaphore = asyncio.Semaphore(1)
        self._active_user_reply_count: int = 0
        self._active_user_reply_turn_count: int = 0
        self._user_reply_done_event: asyncio.Event = asyncio.Event()
        self._user_reply_done_event.set()
        self.topic_started = False
        self.topic_task: asyncio.Task | None = None
        self.agent_task: asyncio.Task | None = None
        mem_db_path = resolve_isolated_path(
            "MEMORY_DB_PATH",
            "/var/lib/ollama-bot/memory_{config_name}.db",
            legacy_fallback_config="config_img",
        )
        self.memory_store = MemoryStore(mem_db_path)
        game_pool_path = resolve_isolated_path(
            "GAME_POOL_DB_PATH",
            "/var/lib/ollama-bot/game_pool_{config_name}.json",
            legacy_fallback_config="config_img",
        )
        self.game_pool_manager = GamePoolManager(game_pool_path)
        self.thread_tracker = ThreadTracker()
        self._agent_action_lock = asyncio.Lock()
        self._last_managed_human_message_ts: float = 0.0
        self._reflection_task: asyncio.Task | None = None
        self.channel_context_cache: dict[int, deque[dict[str, object]]] = defaultdict(
            lambda: deque(
                maxlen=max(
                    cfg_int("MEMORY_LONG_CONTEXT_LINES", 30),
                    cfg_int("MEMORY_SUMMARY_TRIGGER_MESSAGES", 20),
                    cfg_int("CONTEXT_WINDOW_MESSAGES", 20),
                )
            )
        )
        self.emotion_state = EmotionState()
        self._emotion_lock = asyncio.Lock()
        self.user_relationships: dict[int, UserRelationship] = {}
        self._relationship_lock = asyncio.Lock()
        self._emotion_bootstrapped = False
        self._emotion_decay_task: asyncio.Task | None = None
        self._habit_decay_task: asyncio.Task | None = None
        self._emotion_reaction_cooldowns: dict[int, float] = {}
        self._pending_habit_turns: dict[tuple[int | None, int | None], dict[str, object]] = {}
        self.channel_rate_limit_hits: dict[int, deque[float]] = defaultdict(deque)
        self._passive_channel_message_counts: dict[int, int] = {}
        self._other_channel_message_counts: dict[int, int] = {}
        self._other_channel_force_next_reply_cids: set[int] = set()
        self._other_channel_allowed_cache: dict[int, bool] = {}
        self._message_runtime_cache: dict[int, Any] = {}
        # --- 返答タスクキュー ---
        # ゲーム系（20doors/うみがめ）を除く通常返答を優先度付きキューで管理する
        # (priority, seq, message, force_response) の形式で格納し、メンションや返信を最優先で処理する
        self._reply_queue: asyncio.PriorityQueue[tuple[int, int, discord.Message, bool]] = asyncio.PriorityQueue()
        self._reply_queue_seq: int = 0
        self._queue_blocked: bool = False
        self._active_normal_reply_cids: set[int] = set()
        self._queue_worker_task: asyncio.Task | None = None
        self._runtime_tasks: dict[int, asyncio.Task[MessageRuntime]] = {}
        self._img2chan_post_tasks: dict[str, asyncio.Task[Any]] = {}
        self._img2chan_recent_schedule: dict[str, float] = {}
        self._img2chan_thread_states: dict[str, dict[str, object]] = {}
        self._img2chan_active_thread_url: str = ""
        self._img2chan_proactive_post_task: asyncio.Task[Any] | None = None
        # チャンネルごとの最新調査結果キャッシュ
        # img_top5 / x_timeline の結果を保持し、フォローアップ質問の深掘り判定に使う
        self._channel_research_cache: dict[int, dict[str, object]] = {}
        self._runtime_tasks_lock = asyncio.Lock()
        self._factcheck_state_lock = asyncio.Lock()
        self._factcheck_in_progress = False
        self._factcheck_owner_label = ""
        self._factcheck_owner_started_at = 0.0
        self._factcheck_request_kind = ""
        self._twenty_doors_state_lock = asyncio.Lock()
        self._twenty_doors_starting_channels: set[int] = set()
        self._fact_check_menu = app_commands.ContextMenu(
            name="ファクトチェック",
            callback=self.fact_check_message,
        )
        self._summarize_menu = app_commands.ContextMenu(
            name="長文を要約",
            callback=self.summarize_message,
        )
        self._simplify_menu = app_commands.ContextMenu(
            name="わかりやすく解説",
            callback=self.simplify_message,
        )
        self._what_is_this_menu = app_commands.ContextMenu(
            name="これ何？(Web検索)",
            callback=self.what_is_this_message,
        )
        if hasattr(self, "context_image_search"):
            self._image_search_menu = app_commands.ContextMenu(
                name="画像検索",
                callback=self.context_image_search,
            )
        self.umigame_states: dict[int, dict[str, object]] = {}
        self._recent_umigame_questions: deque[str] = deque(maxlen=int(cfg("UMIGAME_RECENT_QUESTION_CACHE_SIZE", 24) or 24))
        self.twenty_doors_states: dict[int, object] = {}
        self._recent_twenty_doors_words: deque[str] = deque(maxlen=int(cfg("TWENTY_DOORS_RECENT_WORD_CACHE_SIZE", 24) or 24))
        self.singing_tasks: dict[int, asyncio.Task[Any]] = {}
        self._singing_lock = asyncio.Lock()


async def setup_ollama_chat(bot: commands.Bot) -> None:
    await bot.add_cog(OllamaChatCog(bot))
