"""AIエージェント・ハーネス 応答観察評価器 (reply_evaluator.py)

フェーズ4. 結果の観察と評価（Observation）において、
LLMから生成された応答や思考（Thought）を観察・精査し、
オウム返し、文末途切れ、思考タグ漏洩、不知スキップ等の妥当性を評価して、
再試行（フェーズ2: Thought への差し戻し）またはフェーズ5（Final Response）への進行を判定する。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .types import EvaluationResult, EvaluationVerdict, Observation, ObservationStatus
from harness.agent.prompt_parser.parser.validation import (
    looks_like_parrot_reply,
    looks_like_prompt_leak,
    looks_like_reasoning_leak,
    looks_like_truncated_reply,
    looks_like_unknown_or_incomprehension_reply,
    should_skip_unknown_reply,
)

log = logging.getLogger("harness.agent.observation.reply_evaluator")


@dataclass
class ReplyObservationResult:
    """応答の観察評価結果"""
    is_valid: bool
    evaluation: EvaluationResult
    needs_retry: bool
    retry_instruction: str
    should_skip_sending: bool = False


class ReplyObservationEvaluator:
    """LLM生成応答の観察・品質評価器"""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or log

    def evaluate_reply(
        self,
        generated_reply: str,
        user_prompt: str,
        *,
        is_primary_channel: bool = False,
        is_direct_mention: bool = False,
    ) -> ReplyObservationResult:
        """生成された応答を観察し、品質評価および次アクション判定を行う。"""
        text = (generated_reply or "").strip()

        # 1. 不知・理解不能スキップ判定 (AGENTS.md 項目77)
        if should_skip_unknown_reply(
            text,
            is_primary_channel=is_primary_channel,
            is_direct=is_direct_mention,
            force_response=is_direct_mention,
        ):
            self.log.info("[reply_evaluator] Incomprehension reply detected; skipping sending.")
            obs = Observation(
                tool_name="reply_generation",
                success=True,
                status=ObservationStatus.SUCCESS,
                output=text,
            )
            eval_res = EvaluationResult(
                verdict=EvaluationVerdict.GOAL_ACHIEVED,
                score=0.5,
                reason="Unknown reply skipped according to policy",
                is_goal_achieved=True,
                needs_rethink=False,
            )
            return ReplyObservationResult(
                is_valid=True,
                evaluation=eval_res,
                needs_retry=False,
                retry_instruction="",
                should_skip_sending=True,
            )

        # 2. オウム返し検知 (AGENTS.md 項目3, 44)
        if looks_like_parrot_reply(text, user_prompt):
            self.log.warning("[reply_evaluator] Parrot reply detected: %r", text)
            eval_res = EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.1,
                reason="Parrot reply detected",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="rethink_with_unique_response",
                rethink_prompt="相手の言葉をそのまま繰り返さず、あなたの意見や独自のリアクションを返してください。",
            )
            return ReplyObservationResult(
                is_valid=False,
                evaluation=eval_res,
                needs_retry=True,
                retry_instruction=eval_res.rethink_prompt,
            )

        # 3. 思考タグ漏洩・プロンプト漏洩検知 (AGENTS.md 項目15)
        if looks_like_reasoning_leak(text) or looks_like_prompt_leak(text):
            self.log.warning("[reply_evaluator] Reasoning/Prompt leak detected: %r", text)
            eval_res = EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.1,
                reason="Reasoning or prompt leak detected",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="sanitize_and_rethink",
                rethink_prompt="思考メモや英語の検証テキストを含めず、キャラクターとしての自然なセリフのみを出力してください。",
            )
            return ReplyObservationResult(
                is_valid=False,
                evaluation=eval_res,
                needs_retry=True,
                retry_instruction=eval_res.rethink_prompt,
            )

        # 4. 文末途切れ検知
        if looks_like_truncated_reply(text):
            self.log.warning("[reply_evaluator] Truncated reply detected: %r", text)
            eval_res = EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.3,
                reason="Truncated sentence detected",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="rethink_complete_sentence",
                rethink_prompt="文末が途中で切れないよう、完全な文章で簡潔に結んでください。",
            )
            return ReplyObservationResult(
                is_valid=False,
                evaluation=eval_res,
                needs_retry=True,
                retry_instruction=eval_res.rethink_prompt,
            )

        # 合格
        obs = Observation(
            tool_name="reply_generation",
            success=True,
            status=ObservationStatus.SUCCESS,
            output=text,
        )
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.GOAL_ACHIEVED,
            score=1.0,
            reason="Reply passed all quality guards",
            is_goal_achieved=True,
            needs_rethink=False,
        )
        return ReplyObservationResult(
            is_valid=True,
            evaluation=eval_res,
            needs_retry=False,
            retry_instruction="",
            should_skip_sending=False,
        )
