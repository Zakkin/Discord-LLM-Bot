"""通常会話返信の公開Mixinを組み立てるエントリーポイント。

このファイルはDiscord通常会話の返信機能で使う `OllamaChatReplyMixin` の公開import先を
維持するための薄い合成レイヤー。返信コアは責務別に以下の7モジュールに分割され、
`reply_core.py` で合成クラス `OllamaChatReplyCoreMixin` として集約されている:
- `reply_core_common.py`: 共通定数、プロキシ関数、例外定義、ユーティリティ
- `reply_core_prompts.py`: システムプロンプト、思考適応、行動スタイル、境界注記 (ReplyPromptMixin)
- `reply_core_runtime.py`: MessageRuntime構築、時刻/天気即時応答、履歴管理 (ReplyRuntimeMixin)
- `reply_core_sending.py`: Discord送信作法、相槌絵文字、直接返信判定 (ReplySendingMixin)
- `reply_core_episode.py`: エピソード記憶、長期記憶、Prospective Memory、要約更新 (ReplyEpisodeMixin)
- `reply_core_model_call.py`: モデル呼び出し、再試行・リカバリ、排他・直列化制御 (ReplyModelCallMixin)
- `reply_core_break.py`: 自発発話・ブレイク返信生成 (ReplyBreakMixin)
- `reply_core.py`: 上記各Mixinの合成クラスおよび司令塔 `_generate_normal_reply`
"""
from __future__ import annotations

import sys as _sys

from ollama_bot.ollama_chat_.ollama_chat_helpers import *
from ollama_bot.ollama_chat_.ollama_chat_helpers import _build_summary_context_lines, _find_last_assistant_message_text, _build_reply_context_user_text, _check_context_flow, _looks_like_role_flip_reply, _looks_too_similar_to_previous_reply, _strip_user_echo_prefix, _build_role_flip_fallback_reply
from front.discord.reply.reply_core import OllamaChatReplyCoreMixin, cfg, cfg_int, cfg_float, cfg_bool, fetch_recent_context_lines, _find_last_assistant_message_text, fetch_recent_x_timeline_items, research_dispatch, extract_and_store_memory, maybe_store_training_candidate, update_user_habit_profile, update_user_interests, _build_summary_context_lines, maybe_update_channel_summary
from harness.agent.prompt_parser.parser.reply_guards import OllamaChatReplyGuardMixin

_sys.modules.setdefault("ollama_bot.ollama_chat_.ollama_chat_reply", _sys.modules[__name__])



class OllamaChatReplyMixin(
    OllamaChatReplyCoreMixin,
    OllamaChatReplyGuardMixin,
):
    """Discord通常会話返信を構成するMixin。"""

