"""AIエージェント・ハーネス 観察評価器 (evaluator.py)

フェーズ4. 結果の観察と評価（Observation）の評価ロジックを担当。
ツールの実行結果がユーザーの要求やタスクの目的を達成しているかを評価し、
「フェーズ5（最終応答）に進む」か「フェーズ2（推論・計画）に戻って再試行するか」
の判定（Verdict）および再思考用プロンプトを生成する。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .loop_detector import LoopDetector
from .types import EvaluationResult, EvaluationVerdict, Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.evaluator")

# 情報不足・見つからないことを示す典型的なレスポンスパターン
_NOT_FOUND_PATTERNS = [
    re.compile(r"見つかりませんでした", re.IGNORECASE),
    re.compile(r"一致する情報(?:は|が)?(?:見つかりませんでした|ありません|存在しません)?", re.IGNORECASE),
    re.compile(r"該当する(?:データ|情報|結果)(?:がありません|は存在しません|は見つかりませんでした)", re.IGNORECASE),
    re.compile(r"お探しの(?:ページ|情報|キーワード)は見つかりませんでした", re.IGNORECASE),
    re.compile(r"検索結果(?:は|が)?\s*0\s*件", re.IGNORECASE),
    re.compile(r"ヒット(?:した|する)?(?:情報|ページ|件数)は(?:ありません|0件)", re.IGNORECASE),
    re.compile(r"no\s+(?:results?|information|matches?)\s+found", re.IGNORECASE),
    re.compile(r"no results? found", re.IGNORECASE),
    re.compile(r"404\s*not\s*found", re.IGNORECASE),
    re.compile(r"情報が取得できませんでした", re.IGNORECASE),
]

# 未知語・スラング検索空振り時の再思考プロンプト
_RETHINK_RETRY_PROMPT = (
    "【再検討指示】直前の検索では十分な情報が見つかりませんでした。"
    "複合語の場合は単語を分解・単純化する（例: 『すげえゴイスー』➜『ゴイスー』または『ゴイスー 意味』）、"
    "あるいは表記揺れを修正して再検索してください。"
)



class ObservationEvaluator:
    """ツールの実行結果を評価し、エージェントの次アクションを決定する評価器"""

    def __init__(
        self,
        loop_detector: LoopDetector | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.loop_detector = loop_detector or LoopDetector()
        self.log = logger or log

    def evaluate(
        self,
        observation: Observation,
        user_intent: str = "",
        tool_arguments: dict[str, Any] | None = None,
        current_turn: int | None = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """単一の観察結果を評価する。

        Args:
            observation: ツール実行結果の観察データ
            user_intent: ユーザーからの要求・プロンプト・達成目標
            tool_arguments: 実行時に渡されたツールの引数
            current_turn: 現在のオーケストレーターターン番号

        Returns:
            EvaluationResult: 判定結果と次ステップ推奨
        """
        # 1. ループ・安全ガードチェック
        is_error = not observation.success or observation.status in (
            ObservationStatus.ERROR,
            ObservationStatus.TIMEOUT,
            ObservationStatus.PERMISSION_DENIED,
            ObservationStatus.RATE_LIMITED,
            ObservationStatus.PROTOCOL_ERROR,
            ObservationStatus.SERVER_ERROR,
            ObservationStatus.BAD_ARGUMENTS,
        )
        loop_res = self.loop_detector.record_and_check(
            tool_name=observation.tool_name,
            arguments=tool_arguments,
            is_error=is_error,
            current_turn=current_turn,
        )

        if loop_res.abort_execution:
            self.log.warning(
                "[observation.evaluator] Abort triggered by loop detector: %s",
                loop_res.message,
            )
            return EvaluationResult(
                verdict=EvaluationVerdict.FATAL_ERROR,
                score=0.0,
                reason=loop_res.message,
                is_goal_achieved=False,
                needs_rethink=False,
                suggested_next_step="abort_and_fallback",
                rethink_prompt=(
                    f"【安全停止】{loop_res.message}\n"
                    "これ以上の外部干渉を行わず、現在の情報のみでユーザーに丁寧にお詫び・状況説明を返答してください。"
                ),
                error_loop_detected=True,
            )

        if loop_res.loop_detected:
            self.log.info(
                "[observation.evaluator] Loop detected (count=%d): %s",
                loop_res.loop_count,
                loop_res.message,
            )
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.2,
                reason=loop_res.message,
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="switch_tool_or_approach",
                rethink_prompt=(
                    f"【警告: ループ検知】{loop_res.message}\n"
                    f"直前に実行したツール '{observation.tool_name}' はこれ以上同一引数で実行しないでください。"
                    "別のアプローチ（別のツールを使用、または検索クエリの変更）を計画してください。"
                ),
                error_loop_detected=True,
            )

        # 2. ステータス別評価
        if observation.status == ObservationStatus.TIMEOUT:
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.1,
                reason=f"ツール '{observation.tool_name}' の実行がタイムアウトしました。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="retry_with_alternative_tool",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' はタイムアウトしました。"
                    "処理が重すぎるか応答がありません。別のツールを使用するか、範囲を絞ったクエリで再試行してください。"
                ),
            )

        if observation.status == ObservationStatus.PERMISSION_DENIED:
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.1,
                reason=f"権限またはサンドボックス制約により実行が拒否されました: {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="avoid_privileged_action",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' の実行は権限制限により拒否されました。"
                    "ホスト環境や保護領域へのアクセスは禁止されています。安全な代替手段を検討してください。"
                ),
            )

        if observation.status == ObservationStatus.RATE_LIMITED:
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.1,
                reason=f"APIのレート制限に達しました: {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="switch_to_offline_or_fallback",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' はレート制限により失敗しました。"
                    "内部知識や記憶データを用いて回答するか、別の情報源を使用してください。"
                ),
            )

        if observation.status == ObservationStatus.LOGIN_WALL:
            return EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.2,
                reason="外部サービスのログインウォールによりコンテンツを取得できませんでした。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="try_alternative_source",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' で取得したページは認証/ログインが必要です。"
                    "公開されている別のWebサイトや代替情報源を検索してください。"
                ),
            )

        if observation.status == ObservationStatus.PROTOCOL_ERROR:
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.1,
                reason=f"MCP/JSON-RPCプロトコルエラー: {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="switch_to_alternative_tool_or_builtin",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' はプロトコルエラーのため失敗しました: {observation.error}\n"
                    "ツールサーバーが無応答またはメソッドが無効です。別ツールを選択するか、内部知識で代替してください。"
                ),
            )

        if observation.status == ObservationStatus.SERVER_ERROR:
            return EvaluationResult(
                verdict=EvaluationVerdict.ALTERNATIVE_TOOL,
                score=0.1,
                reason=f"外部サーバーエラー (5xx): {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="fallback_to_other_service",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' の接続先サーバーで障害（5xxエラー）が発生しています。\n"
                    "一時的に外部サービスが利用できないため、他のツールを使用するか回答方針を変更してください。"
                ),
            )

        if observation.status == ObservationStatus.BAD_ARGUMENTS:
            return EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.2,
                reason=f"ツールの引数不正 (バリデーションエラー): {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="correct_tool_arguments",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' の引数が不正です: {observation.error}\n"
                    "必須パラメータの有無、データ型（文字列・数値・配列）、スキーマを確認して修正してください。"
                ),
            )

        if observation.status == ObservationStatus.ERROR:
            return EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.2,
                reason=f"ツール実行エラー: {observation.error}",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="fix_arguments_and_retry",
                rethink_prompt=(
                    f"ツール '{observation.tool_name}' の実行でエラーが発生しました: {observation.error}\n"
                    "引数のフォーマットや型、パラメータ名が正しいか確認し、修正して再実行してください。"
                ),
            )

        if observation.status == ObservationStatus.EMPTY_RESULT:
            return EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.4,
                reason="ツールは正常終了しましたが、該当する結果が得られませんでした。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="simplify_query_and_retry",
                rethink_prompt=_RETHINK_RETRY_PROMPT,
            )

        # 3. 正常終了（SUCCESS）時の内容評価
        output_text = observation.output.strip()

        # 出力テキストが「見つかりませんでした」系、または短すぎて意味が分からない空振りの判定
        is_not_found = any(p.search(output_text) for p in _NOT_FOUND_PATTERNS)

        # 検索・調査系ツールの判定
        is_search_tool = any(
            k in observation.tool_name.lower()
            for k in ("search", "web", "find", "google", "query", "browse", "wiki", "fact")
        ) or bool(tool_arguments and any(k in tool_arguments for k in ("query", "q", "search_term")))

        # 意味・解説キーワード（例: "とは", "意味", "倒語", "略", "＝", "=" 等）が含まれているか
        has_explanation = any(
            kw in output_text for kw in ("とは", "意味", "倒語", "略", "＝", "=", "由来", "語源", "指す", "こと。")
        )

        # 検索結果として短すぎて意味が分からない（空振りやノイズ）場合
        is_search_too_short = is_search_tool and (len(output_text) < 10 and not has_explanation)

        if is_not_found or is_search_too_short:
            return EvaluationResult(
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.4,
                reason="ツール実行結果に対象情報が見つからない、または情報量が不十分です。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="simplify_query_and_retry",
                rethink_prompt=_RETHINK_RETRY_PROMPT,
            )

        # 十分な情報・単語の意味や概要が得られたと判定（Goal Achieved -> Phase 5 Final Response）
        self.log.info(
            "[observation.evaluator] Tool=%s satisfied goal with output len=%d",
            observation.tool_name,
            len(output_text),
        )
        return EvaluationResult(
            verdict=EvaluationVerdict.GOAL_ACHIEVED,
            score=1.0,
            reason=f"ツール '{observation.tool_name}' から十分な実行結果が得られました。",
            is_goal_achieved=True,
            needs_rethink=False,
            suggested_next_step="proceed_to_final_response",
            rethink_prompt="",
        )
