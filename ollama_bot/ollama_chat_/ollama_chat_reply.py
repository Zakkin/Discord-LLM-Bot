"""通常会話返信の公開Mixinを組み立てるエントリーポイント。

このファイルはDiscord通常会話の返信機能で使う `OllamaChatReplyMixin` の公開import先を
維持するための薄い合成レイヤー。実際の処理は、返信設定・送信・runtime構築、記憶保存、
通常/割り込み返答生成、LLM出力ガードに分けて各 `ollama_chat_reply_*` モジュールへ
分割している。今後AIエージェントが修正するときは、変更したい責務に対応する分割先を
先に確認し、このファイルでは公開クラスの継承関係だけを調整する。
"""
from __future__ import annotations

from .ollama_chat_helpers import *
from .ollama_chat_helpers import (
    _build_summary_context_lines,
    _find_last_assistant_message_text,
    _build_reply_context_user_text,
    _check_context_flow,
    _looks_like_role_flip_reply,
    _looks_too_similar_to_previous_reply,
    _strip_user_echo_prefix,
    _build_role_flip_fallback_reply,
)
from .ollama_chat_reply_core import OllamaChatReplyCoreMixin
from .ollama_chat_reply_generation import OllamaChatReplyGenerationMixin
from .ollama_chat_reply_guards import OllamaChatReplyGuardMixin
from .ollama_chat_reply_memory import OllamaChatReplyMemoryMixin


class OllamaChatReplyMixin(
    OllamaChatReplyCoreMixin,
    OllamaChatReplyMemoryMixin,
    OllamaChatReplyGenerationMixin,
    OllamaChatReplyGuardMixin,
):
    """Discord通常会話返信を構成するMixin。"""
