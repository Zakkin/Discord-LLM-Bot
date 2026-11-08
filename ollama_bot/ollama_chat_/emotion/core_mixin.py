"""
[AI Agent Summary]
このファイルは、機能ごとに分割された感情 Mixin クラス群を束ね、外部から統合された `OllamaChatEmotionMixin` として利用するためのエントリーポイントです。
This file bundles all the separated emotion mixin classes and provides the unified `OllamaChatEmotionMixin` entry point.
"""
from .state_mixin import OllamaChatEmotionStateMixin
from .presence_mixin import OllamaChatEmotionPresenceMixin
from .prompts_mixin import OllamaChatEmotionPromptsMixin
from .scoring_mixin import OllamaChatEmotionScoringMixin
from .memory_mixin import OllamaChatEmotionMemoryMixin
from .commands_mixin import OllamaChatEmotionCommandsMixin

class OllamaChatEmotionMixin(
    OllamaChatEmotionStateMixin,
    OllamaChatEmotionPresenceMixin,
    OllamaChatEmotionPromptsMixin,
    OllamaChatEmotionScoringMixin,
    OllamaChatEmotionMemoryMixin,
    OllamaChatEmotionCommandsMixin,
):
    """感情スコア、関係値、Discord上の名前・自己紹介更新、好感度確認コマンドを担当する統合Mixin。"""
    pass

