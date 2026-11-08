"""AIエージェント・ハーネス 観察評価器 (evaluator.py)

フェーズ4. 結果の観察と評価（Observation）の評価ロジックを担当。
ツールの実行結果がユーザーの要求やタスクの目的を達成しているかを評価し、
「フェーズ5（最終応答）に進む」か「フェーズ2（推論・計画）に戻って再試行するか」
の判定（Verdict）および再思考用プロンプトを生成する。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .loop_detector import LoopDetector
from .types import EvaluationResult, EvaluationVerdict, Observation, ObservationStatus

log = logging.getLogger("harness.agent.observation.evaluator")


class InformationGapType(str, Enum):
    """検知された情報ギャップの種別"""
    COVERAGE = "coverage"        # 主要キーワードの被覆率不足・比較クエリの片落ち
    WHEN = "when"                # 時期・日付（発売日や年号など）の不足
    WHY_HOW = "why_how"          # 原因・理由・仕組み・手順の不足
    SPECS_PRICE = "specs_price"  # 価格・スペック・数値情報の不足


@dataclass
class InformationGapResult:
    """情報ギャップ検知結果"""
    has_gap: bool
    gap_type: InformationGapType | None = None
    reason: str = ""
    rethink_prompt: str = ""


# カバレッジ・実質語抽出におけるストップワード
_COVERAGE_STOP_WORDS: set[str] = {
    "とは", "意味", "について", "教えて", "って", "の", "を", "に", "が", "は",
    "で", "と", "や", "から", "まで", "より", "など", "最新", "情報", "概要",
    "詳細", "まとめ", "一覧", "どういう", "なん", "何", "どれ", "どの",
    "比較", "違い", "いつ", "なぜ", "どうして", "どうやって", "いくら", "何円",
    "発売日", "公開日", "何年", "何月", "何日", "スケジュール", "リリース日",
    "配信日", "開始時期", "予定", "原因", "理由", "仕組み", "方法", "やり方",
    "価格", "値段", "定価", "スペック", "性能", "料金", "費用",
}

# 疑問意図（WHEN）トリガー語および解決パターン
_WHEN_TRIGGERS: list[str] = [
    "いつ", "発売日", "公開日", "何年", "何月", "何日", "最新", "スケジュール",
    "リリース日", "配信日", "開始時期", "予定",
]
_WHEN_RESOLVED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"202\d年"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"(?:\d{1,2}月|\d{4}年)"),
    re.compile(r"(?:春|夏|秋|冬)予定"),
    re.compile(r"(?:リリース|発売|配信|公開)(?:予定|された|される|開始|日)"),
]

# 疑問意図（WHY / HOW）トリガー語および解決パターン
_WHY_HOW_TRIGGERS: list[str] = [
    "なぜ", "原因", "理由", "仕組み", "方法", "やり方", "どうして", "どうやって",
]
_WHY_HOW_RESOLVED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:原因|理由|仕組み|手順|方法)は"),
    re.compile(r"(?:ため|から|によって|による|結果|きっかけ|経緯)"),
    re.compile(r"(?:ステップ|手順|方法|やり方)"),
]

# 疑問意図（SPECS / HOW MUCH）トリガー語および解決パターン
_SPECS_PRICE_TRIGGERS: list[str] = [
    "いくら", "価格", "値段", "定価", "スペック", "性能", "何円", "料金", "費用",
]
_SPECS_PRICE_RESOLVED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\d+(?:,\d{3})*\s*(?:円|万円|億円|ドル|\$|¥|GB|TB|MB|fps|Hz|kg|g|cm|mm|%)"),
    re.compile(r"無料|有料|オープン価格"),
]


def extract_key_terms(text: str) -> list[str]:
    """クエリやユーザー意図から主要キーワード（実質語）を抽出する。"""
    if not text:
        return []

    # 記号を除去
    cleaned = re.sub(r"[?？!！、。・/\\|:;()\[\]{}\"\'「」『』〜~＝=+\-_#@]+", " ", text)
    # ストップワードを長い順に空白置換
    for sw in sorted(_COVERAGE_STOP_WORDS, key=len, reverse=True):
        cleaned = cleaned.replace(sw, " ")

    # 漢字・カタカナ・アルファベット・数字のトークンを抽出
    raw_tokens = re.findall(
        r"[A-Za-z0-9]+|[\u4e00-\u9faf]+|[\u30a1-\u30f6ー]+",
        cleaned,
    )

    terms: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip()
        if not tok or tok in _COVERAGE_STOP_WORDS:
            continue
        # 2文字以上の実質語、または英大文字1文字（A, Bなどの比較対象用）
        if len(tok) >= 2 or (len(tok) == 1 and tok.isupper()):
            if tok not in terms:
                terms.append(tok)
    return terms


def detect_information_gap(
    output_text: str,
    user_intent: str = "",
    tool_arguments: dict[str, Any] | None = None,
) -> InformationGapResult:
    """検索結果の充足度（Information Gap）を検証する。

    クエリ主要語の被覆率チェックおよび疑問意図（WHEN, WHY/HOW, SPECS/PRICE）の
    解決状況を検証し、欠落があれば InformationGapResult を返す。
    """
    query_str = ""
    if tool_arguments:
        for k in ("query", "q", "search_term"):
            val = tool_arguments.get(k)
            if isinstance(val, str) and val.strip():
                query_str = val.strip()
                break

    full_context = f"{user_intent} {query_str}".strip()
    if not full_context:
        return InformationGapResult(has_gap=False)

    # 1. クエリ主要語の被覆率（Coverage）チェック
    key_terms = extract_key_terms(full_context)
    is_comparison = bool(
        re.search(r"(?:比較|違い|vs|VS|対比|どっち|どちら)", full_context)
        or ("と" in full_context and any(kw in full_context for kw in ("比較", "違い", "どっち", "どちら", "差")))
    )

    if key_terms:
        matched_terms = [t for t in key_terms if t.lower() in output_text.lower()]
        missing_terms = [t for t in key_terms if t.lower() not in output_text.lower()]

        # 比較クエリにおける片落ち検知
        if is_comparison and len(key_terms) >= 2 and missing_terms:
            matched_str = "、".join(matched_terms) if matched_terms else "一部の対象"
            missing_str = "、".join(missing_terms)
            return InformationGapResult(
                has_gap=True,
                gap_type=InformationGapType.COVERAGE,
                reason=f"比較クエリにおいて対象「{missing_str}」の情報が欠落しています（片落ち）。",
                rethink_prompt=(
                    f"【再検討指示】直前の検索結果には「{matched_str}」に関する情報は含まれていましたが、"
                    f"比較対象である「{missing_str}」の情報が見当たりませんでした。"
                    f"両方の情報を網羅できるクエリ（例: 『{matched_str} {missing_str} 比較』）で再検索してください。"
                ),
            )

        # 複数主要語指定で1語も含まれていない場合
        if len(key_terms) >= 2 and len(matched_terms) == 0:
            terms_str = "、".join(key_terms)
            return InformationGapResult(
                has_gap=True,
                gap_type=InformationGapType.COVERAGE,
                reason=f"主要キーワード（{terms_str}）が検索結果に含まれていません。",
                rethink_prompt=(
                    f"【再検討指示】直前の検索結果には主要キーワード（{terms_str}）に関する情報が十分に含まれていませんでした。"
                    f"検索クエリを調整して再検索してください。"
                ),
            )

    # 2. 疑問意図（Question Intent）とギャップの照合
    # 時期・時間系 (WHEN)
    if any(kw in full_context for kw in _WHEN_TRIGGERS):
        has_when_resolution = any(p.search(output_text) for p in _WHEN_RESOLVED_PATTERNS)
        if not has_when_resolution:
            return InformationGapResult(
                has_gap=True,
                gap_type=InformationGapType.WHEN,
                reason="時期・日付（発売日や年号など）に関する情報が検索結果に不足しています。",
                rethink_prompt=(
                    "【再検討指示】直前の検索結果には対象の概要は含まれていましたが、"
                    "質問されている「具体的な時期・日付（発売日や年号など）」の情報が見当たりませんでした。"
                    "クエリに「発売日」「2026」「いつ」などの具体語を追加するか、別の切り口で再検索してください。"
                ),
            )

    # 原因・理由系 (WHY / HOW)
    if any(kw in full_context for kw in _WHY_HOW_TRIGGERS):
        has_why_resolution = any(p.search(output_text) for p in _WHY_HOW_RESOLVED_PATTERNS)
        if not has_why_resolution:
            return InformationGapResult(
                has_gap=True,
                gap_type=InformationGapType.WHY_HOW,
                reason="原因・理由・仕組みに関する情報が検索結果に不足しています。",
                rethink_prompt=(
                    "【再検討指示】直前の検索結果には対象の概要は含まれていましたが、"
                    "質問されている「具体的な原因・理由・仕組み」の情報が見当たりませんでした。"
                    "クエリに「原因」「理由」「仕組み」などの具体語を追加するか、別の切り口で再検索してください。"
                ),
            )

    # 数値・スペック・価格系 (SPECS / HOW MUCH)
    if any(kw in full_context for kw in _SPECS_PRICE_TRIGGERS):
        has_specs_resolution = any(p.search(output_text) for p in _SPECS_PRICE_RESOLVED_PATTERNS)
        if not has_specs_resolution:
            return InformationGapResult(
                has_gap=True,
                gap_type=InformationGapType.SPECS_PRICE,
                reason="価格・スペック・数値に関する情報が検索結果に不足しています。",
                rethink_prompt=(
                    "【再検討指示】直前の検索結果には対象の概要は含まれていましたが、"
                    "質問されている「価格・スペックなどの具体的な数値」の情報が見当たりませんでした。"
                    "クエリに「価格」「値段」「スペック」などの具体語を追加するか、別の切り口で再検索してください。"
                ),
            )

    return InformationGapResult(has_gap=False)

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

# 要約タイムアウト等によるフォールバック抜粋を検知するパターン
_FALLBACK_PATTERNS = [
    re.compile(r"要約(?:の)?生成がタイムアウト", re.IGNORECASE),
    re.compile(r"フォールバック(?:抜粋)?", re.IGNORECASE),
    re.compile(r"HTTP\s+fallback", re.IGNORECASE),
    re.compile(r"fallback", re.IGNORECASE),
    re.compile(r"検索結果(?:（抜粋）|\(抜粋\))", re.IGNORECASE),
    re.compile(r"Web検索結果一覧[:：]", re.IGNORECASE),
    re.compile(r"raw_snippet", re.IGNORECASE),
]

# フィクション・キャラクター解説記事の判定パターン
_CHARACTER_PROFILE_PATTERNS = [
    re.compile(r"(?:ピクシブ百科事典|pixiv|ニコニコ大百科|nicovideo|ウィキペディア|Wikipedia|アニヲタWiki|Neoapo)", re.IGNORECASE),
    re.compile(r"(?:登場人物|キャラクター|架空の人物|架空のキャラクター|作中人物|主要人物)"),
    re.compile(r"(?:CV[:：]|声優[:：]|キャスト[:：]|担当声優[:：])"),
    re.compile(r"(?:必殺技|能力値|戦闘能力|アルコバレーノ|所属組織|スリーサイズ|血液型|生年月日)"),
    re.compile(r"(?:身長[:：]\s*\d+|体重[:：]\s*\d+|年齢[:：]\s*\d+)"),
    re.compile(r"(?:アニメ|漫画|マンガ|コミック|ゲーム|ライトノベル|原作|劇場版|単行本|連載)(?:に登場|の登場人物|のキャラクター|の主人公|の主要人物)"),
    re.compile(r"(?:同名のキャラクター|同名人物|曖昧さ回避)"),
]

# ユーザーがその作品・キャラクターについて質問していることを示すパターン
_FICTION_INTENT_PATTERNS = [
    re.compile(r"(?:アニメ|漫画|マンガ|コミック|ゲーム|声優|CV|キャラ|作中|原作|作品|映画|劇場版)", re.IGNORECASE),
    re.compile(r"(?:ピクシブ|pixiv|ニコ百|ニコニコ大百科|大百科|wiki|ウィキペディア|Wikipedia)", re.IGNORECASE),
    re.compile(r"(?:リボーン|REBORN|アルコバレーノ)", re.IGNORECASE),
]


def detect_search_fallback(observation: Observation) -> bool:
    """ツール結果（Web検索等）が要約タイムアウト等によるフォールバック抜粋であるか検知する。"""
    if isinstance(observation.data, dict):
        if bool(observation.data.get("fallback") or observation.data.get("is_fallback")):
            return True
        if str(observation.data.get("source_backend") or "").lower() == "http_fallback":
            return True
        if bool(observation.data.get("timeout_fallback")):
            return True

    text = observation.output or ""
    return any(p.search(text) for p in _FALLBACK_PATTERNS)


def detect_target_name_character_conflict(
    output_text: str,
    target_name: str,
    user_intent: str = "",
) -> bool:
    """Web検索結果が対話相手名と同名の別キャラクター・別人物の辞書記事であるか検証する。

    ユーザー発言がその作品・キャラクターについて尋ねていないにもかかわらず、
    対話相手名（target_name）と同名のアニメ・ゲームキャラクター等の辞書記事がヒットした場合に True を返す。
    """
    t_name = (target_name or "").strip()
    if not t_name:
        return False

    clean_target = re.sub(r"[『』「」\s・さんちゃん様君くん氏]", "", t_name)
    if len(clean_target) < 2:
        return False

    # 出力テキストに対話相手名が含まれるか
    if clean_target not in output_text:
        return False

    # キャラクター解説・辞書記事であるか
    is_char_article = any(p.search(output_text) for p in _CHARACTER_PROFILE_PATTERNS)
    if not is_char_article:
        return False

    # 元のユーザー発言がその作品・キャラクターについて尋ねているか
    user_asks_about_fiction = any(p.search(user_intent) for p in _FICTION_INTENT_PATTERNS)
    if user_asks_about_fiction:
        return False

    return True



class ObservationEvaluator:
    """ツールの実行結果を評価し、エージェントの次アクションを決定する評価器"""

    def __init__(
        self,
        loop_detector: LoopDetector | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.loop_detector = loop_detector or LoopDetector()
        self.log = logger or log

    def _create_and_log_result(
        self,
        observation: Observation,
        verdict: EvaluationVerdict,
        score: float,
        reason: str,
        is_goal_achieved: bool,
        needs_rethink: bool,
        suggested_next_step: str,
        rethink_prompt: str = "",
        error_loop_detected: bool = False,
        fallback_detected: bool = False,
    ) -> EvaluationResult:
        """評価結果モデルを生成し、統一ログを出力する。"""
        res = EvaluationResult(
            verdict=verdict,
            score=score,
            reason=reason,
            is_goal_achieved=is_goal_achieved,
            needs_rethink=needs_rethink,
            suggested_next_step=suggested_next_step,
            rethink_prompt=rethink_prompt,
            error_loop_detected=error_loop_detected,
        )
        self.log.info(
            "[evaluator] Tool=%s goal_satisfied=%s should_continue=%s fallback_detected=%s output_len=%d",
            observation.tool_name,
            res.is_goal_achieved,
            not res.is_goal_achieved,
            fallback_detected,
            len(observation.output),
        )
        return res

    def evaluate(
        self,
        observation: Observation,
        user_intent: str = "",
        tool_arguments: dict[str, Any] | None = None,
        current_turn: int | None = None,
        *,
        target_name: str = "",
        **kwargs: Any,
    ) -> EvaluationResult:
        """単一の観察結果を評価する。

        Args:
            observation: ツール実行結果の観察データ
            user_intent: ユーザーからの要求・プロンプト・達成目標
            tool_arguments: 実行時に渡されたツールの引数
            current_turn: 現在のオーケストレーターターン番号
            target_name: 対話相手の表示名・ニックネーム（同名キャラ混同防止用）

        Returns:
            EvaluationResult: 判定結果と次ステップ推奨
        """
        eff_target_name = (
            target_name
            or str(kwargs.get("target_user_name") or kwargs.get("user_name") or "").strip()
        )
        fallback_detected = detect_search_fallback(observation)

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
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if loop_res.loop_detected:
            self.log.info(
                "[observation.evaluator] Loop detected (count=%d): %s",
                loop_res.loop_count,
                loop_res.message,
            )
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        # 2. ステータス別評価
        if observation.status == ObservationStatus.TIMEOUT:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.PERMISSION_DENIED:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.RATE_LIMITED:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.LOGIN_WALL:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.PROTOCOL_ERROR:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.SERVER_ERROR:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.BAD_ARGUMENTS:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.ERROR:
            return self._create_and_log_result(
                observation=observation,
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
                fallback_detected=fallback_detected,
            )

        if observation.status == ObservationStatus.EMPTY_RESULT:
            return self._create_and_log_result(
                observation=observation,
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.4,
                reason="ツールは正常終了しましたが、該当する結果が得られませんでした。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="simplify_query_and_retry",
                rethink_prompt=_RETHINK_RETRY_PROMPT,
                fallback_detected=fallback_detected,
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
            return self._create_and_log_result(
                observation=observation,
                verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                score=0.4,
                reason="ツール実行結果に対象情報が見つからない、または情報量が不十分です。",
                is_goal_achieved=False,
                needs_rethink=True,
                suggested_next_step="simplify_query_and_retry",
                rethink_prompt=_RETHINK_RETRY_PROMPT,
                fallback_detected=fallback_detected,
            )

        # 3.1 Web検索結果の対話相手名衝突（同名キャラ・別人物混同）検知
        if is_search_tool and eff_target_name:
            is_conflict = detect_target_name_character_conflict(
                output_text=output_text,
                target_name=eff_target_name,
                user_intent=user_intent,
            )
            if is_conflict:
                clean_target = re.sub(r"[『』「」\s・さんちゃん様君くん氏]", "", eff_target_name)
                caution_notice = (
                    f"\n\n※注意: 検索結果の『{clean_target}』は同名の別キャラクターの情報です。"
                    f"目の前の対話相手『{clean_target}』さん本人の属性として混同しないでください。"
                )
                if caution_notice.strip() not in observation.output:
                    observation.output = observation.output.rstrip() + caution_notice

                # 過剰リトライ防止: current_turn >= 3 の場合は安全フォールバック（注意書き付きで最終応答へ進む）
                if current_turn is not None and current_turn >= 3:
                    self.log.info(
                        "[observation.evaluator] Target name collision detected at turn limit (turn=%d). "
                        "Proceeding with caution notice appended.",
                        current_turn,
                    )
                else:
                    self.log.warning(
                        "[observation.evaluator] Target name character collision detected: target=%r. Rejecting as noise.",
                        clean_target,
                    )
                    return self._create_and_log_result(
                        observation=observation,
                        verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                        score=0.3,
                        reason=f"検索結果が対話相手『{clean_target}』と同名の別キャラクター・別人物の記事であり、ユーザーの質問意図と一致していません（ノイズ）。",
                        is_goal_achieved=False,
                        needs_rethink=True,
                        suggested_next_step="retry_with_corrected_query_or_finish",
                        rethink_prompt=(
                            f"【再検討指示】検索結果は対話相手『{clean_target}』と同名の別キャラクター・別人物に関する情報でした。"
                            f"対話相手本人の情報として混同せず、本来の話題に沿って再検索するか、検索を行わずに直接対話してください。"
                        ),
                        fallback_detected=fallback_detected,
                    )

        # 4. 検索結果の充足度評価（Information Gap / 被覆率検知）
        if is_search_tool:
            # 過剰リトライ防止: current_turn >= 3 の場合は無限ループ防止のため安全フォールバック
            if current_turn is not None and current_turn >= 3:
                self.log.info(
                    "[observation.evaluator] Turn limit threshold reached (turn=%d). "
                    "Skipping information gap retry and proceeding to final response.",
                    current_turn,
                )
            else:
                gap_res = self._detect_information_gap(
                    output_text=output_text,
                    user_intent=user_intent,
                    tool_arguments=tool_arguments,
                )
                if gap_res.has_gap:
                    self.log.info(
                        "[observation.evaluator] Information gap detected (%s): %s",
                        gap_res.gap_type,
                        gap_res.reason,
                    )
                    return self._create_and_log_result(
                        observation=observation,
                        verdict=EvaluationVerdict.RETRY_WITH_CORRECTION,
                        score=0.5,
                        reason=gap_res.reason,
                        is_goal_achieved=False,
                        needs_rethink=True,
                        suggested_next_step="refine_query_for_gap",
                        rethink_prompt=gap_res.rethink_prompt,
                        fallback_detected=fallback_detected,
                    )

        # 十分な情報・単語の意味や概要が得られたと判定（Goal Achieved -> Phase 5 Final Response）
        self.log.info(
            "[observation.evaluator] Tool=%s satisfied goal with output len=%d",
            observation.tool_name,
            len(output_text),
        )
        return self._create_and_log_result(
            observation=observation,
            verdict=EvaluationVerdict.GOAL_ACHIEVED,
            score=1.0,
            reason=f"ツール '{observation.tool_name}' から十分な実行結果が得られました。",
            is_goal_achieved=True,
            needs_rethink=False,
            suggested_next_step="proceed_to_final_response",
            rethink_prompt="",
            fallback_detected=fallback_detected,
        )

    def _detect_information_gap(
        self,
        output_text: str,
        user_intent: str = "",
        tool_arguments: dict[str, Any] | None = None,
    ) -> InformationGapResult:
        """検索結果の充足度（Information Gap）を検証する。"""
        return detect_information_gap(
            output_text=output_text,
            user_intent=user_intent,
            tool_arguments=tool_arguments,
        )
