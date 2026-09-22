"""OllamaChat関連モジュールで共有する実行状態、感情状態、関係値のデータ型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..common.emotion_helpers import EMOTION_KEYS


@dataclass
class MediaBundle:
    images: list[str]
    prompt_parts: list[str]
    analysis_text: str


@dataclass
class MessageRuntime:
    cid: Optional[int]
    guild_id: Optional[int]
    user_id: Optional[int]
    original_user_text: str
    effective_user_text: str
    context_lines: list[str]
    media: MediaBundle
    bot_user_id: Optional[int]
    memory_context_lines: list[str] = field(default_factory=list)
    is_weather_reply: bool = False
    is_umigame_reply: bool = False
    is_twenty_doors_reply: bool = False
    working_context: dict[str, object] = field(default_factory=dict)
    pending_future_prompts: list[dict[str, object]] = field(default_factory=list)
    selected_action_style: str = ""
    habit_context_key: str = ""
    reply_research_decision: dict[str, object] = field(default_factory=dict)
    utility_models_used: set[str] = field(default_factory=set)


@dataclass
class EmotionState:
    scores: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in EMOTION_KEYS})
    dominant_emotion: str = "neutral"
    appraisal: str = "大きな出来事とは受け取っていない。"
    reason: str = "平常状態"
    mood: str = "neutral"
    mood_reason: str = "特になし"
    boredom: float = 0.0
    loneliness: float = 0.0
    curiosity: float = 0.0
    tension: float = 0.0
    fatigue: float = 0.0
    vigilance: float = 0.0
    shame: float = 0.0
    excitement: float = 0.0
    action_policy: str | None = None
    last_thought_ts: float = 0.0
    last_human_message_ts: float = 0.0
    last_proactive_post_ts: float = 0.0
    last_agent_tick_ts: float = 0.0
    recent_topic_hint: str = ""
    last_trigger_text: str = ""
    last_nickname_update_ts: float = 0.0
    last_activity_update_ts: float = 0.0
    last_applied_nickname: str = ""
    last_applied_activity: str = ""
    last_applied_bio: str = ""
    last_bio_update_ts: float = 0.0
    base_display_name: str = ""
    last_decay_ts: float = 0.0


@dataclass
class UserRelationship:
    affinity: float = 0.0
    trust: float = 0.0
    affection: float = 0.0
    hatred: float = 0.0
    thought: str = ""
    last_interaction_ts: float = 0.0
    last_thought_generated_ts: float = 0.0
    interaction_count: int = 0


from collections import defaultdict, deque
import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import discord
    from discord import app_commands
    from discord.ext import commands
    from ..common.memory_store import MemoryStore
    from ..common.game_pool_manager import GamePoolManager


class OllamaChatProtocol:
    bot: Any
    semaphore: asyncio.Semaphore
    topic_started: bool
    topic_task: asyncio.Task | None
    agent_task: asyncio.Task | None
    memory_store: Any
    game_pool_manager: Any
    _agent_action_lock: asyncio.Lock
    _last_managed_human_message_ts: float
    _reflection_task: asyncio.Task | None
    channel_context_cache: dict[int, deque[dict[str, object]]]
    emotion_state: EmotionState
    _emotion_lock: asyncio.Lock
    user_relationships: dict[int, UserRelationship]
    _relationship_lock: asyncio.Lock
    _emotion_bootstrapped: bool
    _emotion_decay_task: asyncio.Task | None
    _habit_decay_task: asyncio.Task | None
    _relationship_thought_task: asyncio.Task | None
    _emotion_reaction_cooldowns: dict[int, float]
    _pending_habit_turns: dict[tuple[int | None, int | None], dict[str, object]]
    channel_rate_limit_hits: dict[int, deque[float]]
    _reply_queue: Any
    _reply_queue_seq: int
    _queue_blocked: bool
    _queue_worker_task: asyncio.Task | None
    _runtime_tasks: dict[int, asyncio.Task[MessageRuntime]]
    _img2chan_post_tasks: dict[str, asyncio.Task[Any]]
    _img2chan_recent_schedule: dict[str, float]
    _img2chan_thread_states: dict[str, dict[str, object]]
    _img2chan_active_thread_url: str
    _channel_research_cache: dict[int, dict[str, object]]
    _runtime_tasks_lock: asyncio.Lock
    _factcheck_state_lock: asyncio.Lock
    _factcheck_in_progress: bool
    _factcheck_owner_label: str
    _factcheck_owner_started_at: float
    _factcheck_request_kind: str
    _twenty_doors_state_lock: asyncio.Lock
    _twenty_doors_starting_channels: set[int]
    twenty_doors_games: dict[int, Any]
    twenty_doors_states: dict[int, Any]
    _recent_twenty_doors_words: deque[str]
    umigame_states: dict[int, Any]
    _recent_umigame_questions: deque[str]
    singing_tasks: dict[int, asyncio.Task[Any]]
    _other_channel_unreplied_counts: dict[int, int]
    _other_channel_target_counts: dict[int, int]
    _singing_lock: asyncio.Lock
    _fact_check_menu: Any
    _summarize_menu: Any
    _simplify_menu: Any
    _what_is_this_menu: Any

    async def _load_emotion_state(self) -> None: pass
    def _save_emotion_state(self) -> None: pass
    async def _load_user_relationships(self) -> None: pass
    async def _save_user_relationships(self) -> None: pass
    async def _sync_emotion_presence(self) -> None: pass
    async def _bootstrap_emotion_base_name(self) -> None: pass
    async def _get_or_build_runtime(self, message: Any) -> MessageRuntime | None: raise NotImplementedError
    async def _update_emotion_state(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _score_emotion_with_appraisal(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _maybe_add_scored_reaction(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _handle_active_20doors_message(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _handle_active_umigame_message(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _should_ignore_non_twenty_doors_message(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _is_umigame_reply_message(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _is_twenty_doors_reply_message(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _generate_reply_pipeline(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _post_to_img2chan(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _managed_channel_ids(self) -> set[int]: return set()
    def _append_current_user_message(self, *args: Any, **kwargs: Any) -> Any: pass
    def _append_sent_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _safe_add_reaction(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _safe_remove_own_reaction(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _send_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _apply_retry_guards(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _store_and_update_summary(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _get_emotion_snapshot(self) -> tuple[dict[str, float], str, str]: return ({}, "", "")
    async def _get_user_relationship_snapshot(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _decay_user_relationships(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _answer_umigame(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _init_singing_state(self) -> None: pass
    def is_singing_active_in_channel(self, channel_id: int) -> bool: return False
    async def _sing_song_workflow(self, *args: Any, **kwargs: Any) -> Any: pass
    async def fact_check_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def summarize_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def simplify_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def what_is_this_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _maybe_block_special_action_for_interaction(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _maybe_block_special_action_for_message(self, *args: Any, **kwargs: Any) -> bool: return False
    def _requester_label_from_message(self, *args: Any, **kwargs: Any) -> str: return ""
    async def _try_begin_factcheck(self, *args: Any, **kwargs: Any) -> bool: return True
    def _build_factcheck_busy_text(self, *args: Any, **kwargs: Any) -> str: return ""
    def _resolve_special_action_mode_for_target(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _build_target_block(self, *args: Any, **kwargs: Any) -> str: return ""
    async def _call_special_action_model(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _reply_with_embed_or_fallback(self, *args: Any, **kwargs: Any) -> Any: pass
    def _inject_message_context_cache(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _store_special_action_memory(self, *args: Any, **kwargs: Any) -> Any: pass
    def _extract_what_is_this_query(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _extract_fact_check_search_query(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _call_model(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _pick_dominant_emotion(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _ensure_relationship_state(self) -> None: pass
    def _build_relationship_prompt_for_user(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _get_emotion_memory_lines(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _build_emotion_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _build_emotion_reason_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _derive_mood_from_state(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _trim_memory_context_lines(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _run_deep_reflection_once(self, *args: Any, **kwargs: Any) -> Any: pass
    def _mark_future_prompts_triggered(self, *args: Any, **kwargs: Any) -> Any: pass
    def _remember_pending_habit_turn(self, *args: Any, **kwargs: Any) -> Any: pass
    def _select_retry_backup_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _call_with_typing(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _reply_system_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _reply_retry_num_predict(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _get_preferred_memory_emotion_tags(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _get_current_reply_social_guidance(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _prepend_social_guidance_to_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _build_reply_options(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _call_ollama_with_error_handling(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _select_action_style(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _build_action_style_instruction(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _format_user_thought_for_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _get_core_beliefs_for_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _maybe_store_episode_memory(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _generate_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _generate_reply_with_guards(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _try_quick_replies(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _check_rate_limit(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _check_channel_rate_limit(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _update_runtime_with_context(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _update_memory_after_reply(self, *args: Any, **kwargs: Any) -> Any: pass
    def _apply_action_style_to_prompt(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _format_reply_deliberation_note(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _reply_num_predict(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _research_results_error_text(self, *args: Any, **kwargs: Any) -> str | None: return None
    async def _end_factcheck(self, *args: Any, **kwargs: Any) -> Any: pass
    def _build_dynamic_system_prompt(self, *args: Any, **kwargs: Any) -> str: return ""
    def _requester_label_from_interaction(self, *args: Any, **kwargs: Any) -> str: return ""
    def _build_time_rhythm_guidance(self, *args: Any, **kwargs: Any) -> str: return ""
    def _inject_context_cache(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _handle_time(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _handle_weather(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _generate_break_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _generate_normal_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _send_provisional_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    async def _send_followup_reply(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    _current_background_llm_task: asyncio.Task[Any] | None = None
    async def _emotion_decay_loop(self) -> None: pass
    async def _relationship_thought_loop(self) -> None: pass
    async def _topic_loop(self) -> None: pass
    async def _agent_tick_loop(self) -> None: pass
    async def _reflection_loop(self) -> None: pass
    async def _run_reflection_once(self) -> None: pass
    async def _update_agent_needs(self, *args: Any, **kwargs: Any) -> None: pass
    async def _execute_inner_monologue_and_learn(self) -> None: pass
    async def _proactive_action(self) -> None: pass
    def _clean_img_thread_fragment(self, *args: Any, **kwargs: Any) -> str: return ""
    def _clean_img2chan_learning_text(self, *args: Any, **kwargs: Any) -> str: return ""
    async def _run_emotion_update_for_message(self, *args: Any, **kwargs: Any) -> Any: pass
    async def _run_img2chan_followup_reply_task(self, *args: Any, **kwargs: Any) -> Any: pass
    def _prune_img2chan_post_schedules(self) -> None: pass
    def _ensure_img2chan_thread_tracking_state(self) -> Any: raise NotImplementedError
    def _img2chan_active_thread_limit(self) -> int: return 1
    def _img2chan_thread_state(self, url: str) -> dict[str, Any]: raise NotImplementedError
    def _img2chan_thread_monitor_max_replies(self) -> int: return 100
    def _img2chan_thread_monitor_poll_sec(self) -> float: return 15.0
    def _img2chan_thread_dropped(self, *args: Any, **kwargs: Any) -> bool: return False
    def _match_img2chan_pending_own_posts(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: return []
    def _discover_img2chan_quote_reply_candidates(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: return []
    def _schedule_img2chan_followup_reply_tasks(self, *args: Any, **kwargs: Any) -> None: pass
    async def _maybe_learn_from_img2chan_thread_feedback(self, *args: Any, **kwargs: Any) -> Any: pass
    def _img2chan_post_context_replies(self) -> int: return 10
    def _img2chan_track_known_replies(self, *args: Any, **kwargs: Any) -> None: pass
    def _register_img2chan_own_post(self, *args: Any, **kwargs: Any) -> None: pass
    def _remember_img2chan_pending_own_post(self, *args: Any, **kwargs: Any) -> None: pass
    async def _store_img2chan_post_record(self, *args: Any, **kwargs: Any) -> None: pass
    async def _call_img2chan_ollama_json(self, *args: Any, **kwargs: Any) -> Any: raise NotImplementedError
    def _is_twenty_doors_busy_channel(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _stop_singing_from_message(self, *args: Any, **kwargs: Any) -> bool: return False
    def _is_reply_investigate_request(self, *args: Any, **kwargs: Any) -> bool: return False
    def _extract_reply_investigate_instruction(self, *args: Any, **kwargs: Any) -> str: return ""
    async def _handle_reply_investigate(self, *args: Any, **kwargs: Any) -> Any: pass
    def _mark_human_activity(self, *args: Any, **kwargs: Any) -> None: pass
    async def _is_twenty_doors_start_request(self, *args: Any, **kwargs: Any) -> bool: return False
    async def _start_20doors_from_message(self, *args: Any, **kwargs: Any) -> bool: return False

