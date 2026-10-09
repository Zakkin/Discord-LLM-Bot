"""ティア表パーティゲームのデータモデル定義。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Final, Literal

TierRank = Literal["S", "A", "B", "C", "D"]

TIER_RANKS: Final[list[TierRank]] = ["S", "A", "B", "C", "D"]

TIER_SCORES: Final[dict[str, int]] = {
    "S": 4,
    "A": 3,
    "B": 2,
    "C": 1,
    "D": 0,
}

TIER_EMOJIS: Final[dict[str, str]] = {
    "S": "🟥",
    "A": "🟧",
    "B": "🟨",
    "C": "🟩",
    "D": "🟦",
}

TIER_LABELS: Final[dict[str, str]] = {
    "S": "最高・絶対これ",
    "A": "かなり好き・優秀",
    "B": "普通・アリ・定番",
    "C": "微妙・なくてもいい",
    "D": "無理・最下位・地雷",
}

TIER_COLORS: Final[dict[str, int]] = {
    "S": 0xFF4500,  # オレンジレッド
    "A": 0xFF8C00,  # ダークオレンジ
    "B": 0xFFD700,  # ゴールド
    "C": 0x32CD32,  # ライムグリーン
    "D": 0x1E90FF,  # ドジャーブルー
}


class GamePhase(str, Enum):
    LOBBY = "lobby"
    CREATING_ANSWERS = "creating_answers"  # 1. 正解を作る
    ROUND_GUESS = "round_guess"            # 2. お題公開 & 3. 予想する
    ROUND_RESULT = "round_result"          # 4. 結果発表
    GAME_OVER = "game_over"


_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:option|item|choice|選択肢|項目|候補|候補者|番号|No\.?|ナンバー)[\s_\-・]*[0-9a-zA-Z一二三四五0-9０-９]+$",
    re.IGNORECASE,
)

# 客観的知識クイズ・事実・分類系お題（個人の好みの議論にならないもの）を普遍的に排除する正規表現
_OBJECTIVE_FACT_TOPIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:似ている|似た|の仲間|の種類|の生息|学名|分類|何科|何類|特徴|共通点|正解|正誤|何年|何歳|人口|面積|試験|テスト|クイズ)",
    re.IGNORECASE,
)

# 日本語助詞・ひらがなチェック用正規表現（自然な文構造であることの確認）
_HIRAGANA_RE: Final[re.Pattern[str]] = re.compile(r"[\u3040-\u309F]")

# 中国語（簡体字）または中国語特有語彙の検知
_CHINESE_WORDS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:动物|类别|游戏|玩家|选项|设置|回答|主题|内容|比较|最喜|选择)",
    re.IGNORECASE,
)

# カタカナ音韻規則違反（先頭に長音・促音・拗音、または長音の連続など日本語として破綻している単語）
_INVALID_KANA_PHONETICS_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ーッャュョァィゥェォヮ]|ー{2,}|[^\w\s\(\)（）ー\-・\/／&＆〜~～…『』「」]"
)

# プレイヤーをAI扱いする表現や、Botシステム・記憶DBを直接言及するメタ表現を排除する正規表現
_AI_META_TOPIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:AIとして|ボットとして|AIなら|人工知能|データベース|プロンプト|システムログ|AIの記憶|Ollama|機械学習|LLM|モデルとして)",
    re.IGNORECASE,
)

# 日本語の外来語音韻として著しく不自然な表記や誤字（例: ミフテリー等）を排除する正規表現
_UNNATURAL_KANA_PHONETICS_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:フ[テチツケコサシスセソ]|ミフテリー)",
    re.IGNORECASE,
)


def _is_cjk_simplified_char(c: str) -> bool:
    """CJK統合漢字の範囲（0x4E00〜0x9FFF）で、日本語文字コード（cp932）に存在しない簡体字かを判定する。"""
    code = ord(c)
    if 0x4E00 <= code <= 0x9FFF:
        try:
            c.encode("cp932")
        except UnicodeEncodeError:
            return True
    return False


def _contains_simplified_chinese(text: str) -> bool:
    """文字列に簡体字中国語や中国語特有語彙が含まれているかを判定する。"""
    if _CHINESE_WORDS_RE.search(text):
        return True
    return any(_is_cjk_simplified_char(c) for c in text)


# お題と選択肢の自己包含チェックで除外する一般的な修飾語・疑問詞
_COMMON_MODIFIERS: Final[set[str]] = {
    "最高", "最強", "一番", "定番", "人気", "もしも", "日常", "絶対",
    "好き", "おすすめ", "欲しい", "選ぶ", "どれ", "何", "誰", "どこ",
}


@dataclass(slots=True)
class TopicData:
    """お題と5つの選択肢。"""
    topic: str
    options: list[str]
    category: str = "一般"

    def is_valid(self) -> bool:
        """選択肢が5つあり、空文字・重複・数字のみ・記号のみ・プレースホルダー・簡体字・自己包含・音韻破綻・AIメタ等がないかを検証。"""
        if not self.topic or not self.topic.strip():
            return False
        topic_clean = self.topic.strip()

        # 1. 簡体字・中国語の混入チェック（お題・カテゴリ・選択肢すべて）
        if _contains_simplified_chinese(topic_clean) or _contains_simplified_chinese(self.category):
            return False

        # 2. 自然な日本語文構造チェック（お題にひらがなが含まれているか）
        if not _HIRAGANA_RE.search(topic_clean):
            return False

        # 3. 客観的知識クイズ・事実当て系お題（個人の好みの議論にならないもの）の普遍的排除
        if _OBJECTIVE_FACT_TOPIC_RE.search(topic_clean):
            return False

        # 4. プレイヤーをAI扱いするメタ表現・Bot内部言及の排除
        if _AI_META_TOPIC_RE.search(topic_clean) or _AI_META_TOPIC_RE.search(self.category):
            return False

        # 5. 不自然なカタカナ音韻・誤字の排除
        if _UNNATURAL_KANA_PHONETICS_RE.search(topic_clean):
            return False

        # 6. 選択肢の数と重複チェック
        if len(self.options) != 5:
            return False
        cleaned = [opt.strip() for opt in self.options if opt.strip()]
        if len(cleaned) != 5 or len(set(cleaned)) != 5:
            return False

        for opt in cleaned:
            # 簡体字・中国語混入チェック
            if _contains_simplified_chinese(opt):
                return False

            # AIメタ表現・内部システム言及チェック
            if _AI_META_TOPIC_RE.search(opt):
                return False

            # 不自然なカタカナ音韻・誤字チェック
            if _UNNATURAL_KANA_PHONETICS_RE.search(opt):
                return False

            # 数字のみ（半角 "1", "2", "10" 等、全角 "１", "２" 等）は無効
            if opt.isdigit() or opt.isnumeric():
                return False

            # アルファベット1文字のみ（"A", "b" 等）は選択肢ではなく記号番号のため無効
            if len(opt) == 1 and opt.isascii() and opt.isalpha():
                return False

            # 「選択肢1」「項目A」「Option 1」等の無意味なプレースホルダーは無効
            if _PLACEHOLDER_RE.match(opt):
                return False

            # 記号を除去した結果が数字のみ（"1.", "#2", "(3)" 等）
            alnum_only = "".join(c for c in opt if c.isalnum())
            if alnum_only.isdigit() or alnum_only.isnumeric():
                return False

            # 英数字や日本語（漢字・ひらがな・カタカナ等）が一切含まれていない（記号のみ）
            if not alnum_only:
                return False

            # 長さ制限: ティア表のUI視認性と意味の成立（1文字ひらがな/カタカナや、長すぎる文章を排除）
            if len(opt) < 2 and not (len(opt) == 1 and 0x4E00 <= ord(opt) <= 0x9FFF):
                return False
            if len(opt) > 25:
                return False

            # カタカナ音韻規則違反（先頭に「ッ」「ー」「ャ」等、または「ーー」など破綻した文字列）
            if _INVALID_KANA_PHONETICS_RE.search(opt):
                return False

            # 5. お題そのものの主題名詞が選択肢に入っている自己包含（同語反復）のチェック
            # 例: お題「〇〇にトッピングしたいもの」に対して選択肢「〇〇」
            if len(opt) >= 2 and opt in topic_clean and opt not in _COMMON_MODIFIERS:
                return False

        return True


@dataclass(slots=True)
class PlayerState:
    """参加プレイヤーの状態。"""
    user_id: int
    display_name: str
    mention: str
    total_score: int = 0
    rounds_as_host: int = 0
    rounds_as_guesser: int = 0
    secret_topic: TopicData | None = None
    shuffled_options: list[str] = field(default_factory=list)
    secret_submission: PlayerSubmission | None = None
    perfect_count: int = 0


@dataclass(slots=True)
class PlayerSubmission:
    """プレイヤーが提出したティア表。"""
    user_id: int
    user_name: str
    tier_assignments: dict[str, str]  # 選択肢名 -> "S" | "A" | "B" | "C" | "D"
    submitted_at: float


@dataclass(slots=True)
class RoundResult:
    """1ラウンドの結果データ。"""
    round_number: int
    host_id: int
    host_name: str
    topic: TopicData
    host_tiers: dict[str, str]
    shuffled_options: list[str] = field(default_factory=list)
    guesses: dict[int, dict[str, str]] = field(default_factory=dict)
    round_scores: dict[int, int] = field(default_factory=dict)
    item_diffs: dict[int, dict[str, int]] = field(default_factory=dict)
    exact_matches: dict[int, int] = field(default_factory=dict)
    perfect_users: list[int] = field(default_factory=list)
    host_bonus_score: int = 0


@dataclass(slots=True)
class CompatibilityPair:
    """2人間の相性度診断結果。"""
    user_a_id: int
    user_a_name: str
    user_b_id: int
    user_b_name: str
    score: float  # 0.0 ~ 100.0%
    total_comparisons: int
    exact_matches: int
    title: str
    comment: str


def is_valid_tier_assignment(
    tier_assignments: dict[str, str],
    expected_options: list[str] | None = None,
) -> bool:
    """各ティア（S, A, B, C, D）が重複なく1つずつ設定されているかを検証する。"""
    if len(tier_assignments) != 5:
        return False
    if expected_options is not None:
        if set(tier_assignments.keys()) != set(expected_options):
            return False
    ranks = list(tier_assignments.values())
    return len(ranks) == 5 and len(set(ranks)) == 5 and set(ranks) == set(TIER_RANKS)

