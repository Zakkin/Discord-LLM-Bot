"""ユーザー発言内の不明点・作品評価・感想の検知、記憶DB検索（ステップ1）、および不明時フォールバック（ステップ3）を担当するモジュール。"""
from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from harness.agent.memory_manager import MemoryStore

log = logging.getLogger("ollama_bot.common.web_research.topic_investigation")

# 先頭の前置き接続詞・フィラーパターン
_LEADING_FILLER_PATTERN = re.compile(
    r"^(?:つまり|要するに|ちなみに|ところで|じゃあ|では|そういえば|結局|ぶっちゃけ|っていうか|ていうか|というか|なお|あと)[\s、,]*",
    re.IGNORECASE,
)

# 評価・感想・疑問を表すキーワードパターン
_OPINION_PATTERN = re.compile(
    r"(?:"
    r"どう(?:[？?]|なの|なん|ですか|かしら|(?:思|おも)[いうえおっわ]|考[ええっ])|"
    r"どんな[感風]じ|"
    r"(?:面白|おもしろ|楽し|たのし|つまらな|おもんな|しんど|だる|難し|むずかし|簡単|かんたん)[いくいそなたさ]|"
    r"(?:クソ|微妙|重い|重さ|重くて|退屈|神ゲー|名作|傑作|良作|駄作|クソゲー)|"
    r"(?:評判|評価|レビュー|おすすめ|オススメ)|"
    r"(?:勝てる|稼げる|儲かる|どっちがいい|どれがいい|どっちが良い|どれが良い)"
    r")",
    re.IGNORECASE,
)

# 比較・選択を表すキーワードパターン（例: どっち、どちら、どれ、どのほう等）
_COMPARISON_CHOICE_PATTERN = re.compile(
    r"(?:どっち|どちら|どれ|どの(?:方|ほう)|いずれ)",
    re.IGNORECASE,
)

# パート・側面を表すキーワード（例: 学園パート、DLC、戦闘、ストーリー等）
_ASPECT_KEYWORDS_PATTERN = re.compile(
    r"(?:学園パート|ユースドラマ|DLC|追加コンテンツ|ストーリー|シナリオ|戦闘|バトル|ミニゲーム|BGM|グラフィック|システム|エンディング|序盤|中盤|終盤|後半|前編|後編|パート|編|章)",
    re.IGNORECASE,
)

# 行動・ルール・依頼・勧誘・禁止を表すキーワード（評価問い合わせではない文脈）
_RULE_OR_ACTION_PATTERN = re.compile(
    r"(?:"
    r"約束|"
    r"守[ってりる]|"
    r"使わな[いいて]|"
    r"(?:会話|話|遊)[しよす]|"
    r"(?:しよう|しましょう|しようよ|しような)|"
    r"(?:してね|してください|してほしい|してくれ)|"
    r"(?:ようにする|ようにして|ようにしよう)|"
    r"(?:禁止|ダメ|だめ|厳禁|やめて|やめよう|やめな|避けて|控えて)|"
    r"(?:べき|べからず)"
    r")",
    re.IGNORECASE,
)

# 副詞的用法（「楽しく会話」「面白く話す」等）の検出パターン
_ADVERBIAL_OPINION_PATTERN = re.compile(
    r"(?:楽し|面白|おもしろ|たのし)く[\s、]*(?:会話|話|遊|過ご|や|行|暮|生|生きて|やって)",
    re.IGNORECASE,
)


def detect_topic_opinion_inquiry(user_text: str) -> dict[str, Any]:
    """
    ユーザー発言が特定の作品・事物・パートに対する感想・評価・疑問であるかを判定する。
    該当する場合は対象の主題（target_subject）、観点（target_aspect）、検索クエリを返す。
    """
    text = str(user_text or "").strip()
    if not text:
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": "",
            "search_query": "",
            "is_comparison": False,
        }

    # 返信プレフィックス（（.+?への返信））の除去
    text = re.sub(r"^（[^（）\n]{1,120}への返信）[\s\n]*", "", text).strip()
    if not text:
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": "",
            "search_query": "",
            "is_comparison": False,
        }

    # 前置きの除去
    trimmed = _LEADING_FILLER_PATTERN.sub("", text).strip()

    # 行動・ルール・約束・勧誘・依頼・禁止などの文脈であれば評価問い合わせではない
    if _RULE_OR_ACTION_PATTERN.search(trimmed):
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": "",
            "search_query": "",
            "is_comparison": False,
        }

    # 「楽しく会話」「面白く話す」のような副詞的用法をトリミングした上で評価語を探す
    opinion_check_text = _ADVERBIAL_OPINION_PATTERN.sub("", trimmed)

    # 評価・感想キーワードの検出
    opinion_match = _OPINION_PATTERN.search(opinion_check_text)
    found_opinion: str = opinion_match.group(0) if opinion_match else ""

    if not found_opinion:
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": "",
            "search_query": "",
            "is_comparison": False,
        }

    # 観点（パート等）の検出
    aspect_match = _ASPECT_KEYWORDS_PATTERN.search(trimmed)
    target_aspect = aspect_match.group(0) if aspect_match else ""

    # 主題（作品名・事物名）の抽出
    target_subject = ""
    is_comparison = False

    # 1. 比較・選択疑問文の場合、比較語（どっち、どれ等）の手前にある名詞句を最優先で主題とする
    comparison_match = _COMPARISON_CHOICE_PATTERN.search(trimmed)
    if comparison_match:
        before_comp = trimmed[:comparison_match.start()].strip()
        before_comp = re.sub(r"(?:の中で|なら|だと|で|は|って|、)+$", "", before_comp).strip()
        m_comp_subj = re.search(
            r"([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_、／/\s]{2,40})$",
            before_comp,
        )
        if m_comp_subj:
            cand = m_comp_subj.group(1).strip()
            if cand != target_aspect:
                target_subject = cand
                is_comparison = True

    # 2. 比較から取れなかった場合：助詞（は、って、の、における、について、に関して）または読点（、）で区切られた先頭の名詞句
    if not target_subject:
        subject_match = re.match(
            r"^(?:あの|その|この)?([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_]{2,30}?)(?:(?:は|の|って|、)|における|について(?:は)?|に関して(?:は)?|\s)",
            trimmed,
        )
        if subject_match:
            cand = subject_match.group(1).strip()
            if cand != target_aspect:
                target_subject = cand

    # 3. 文頭から取れなかった場合、観点の前の名詞句を探す
    if not target_subject and target_aspect:
        before_aspect = trimmed.split(target_aspect, 1)[0].strip()
        m_before = re.search(
            r"([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_]{2,30}?)(?:(?:は|の|って|、|\s)|について|に関して)*$",
            before_aspect,
        )
        if m_before:
            cand = m_before.group(1).strip()
            if cand != target_aspect:
                target_subject = cand

    # 主題も観点も見つからない場合は単なる一般的な感想・独り言とみなす
    if not target_subject and not target_aspect:
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": found_opinion,
            "search_query": "",
            "is_comparison": False,
        }

    # 検索クエリの構築
    query_parts: list[str] = []
    if target_subject:
        query_parts.append(target_subject)
    if target_aspect:
        query_parts.append(target_aspect)

    if is_comparison:
        # 比較の場合は比較・おすすめ・評判を組み合わせる
        if found_opinion and found_opinion not in {"どっちがいい", "どれがいい", "どっちが良い", "どれが良い"}:
            query_parts.extend([found_opinion, "比較"])
        else:
            query_parts.extend(["比較", "おすすめ"])
    else:
        query_parts.append("評判")

    search_query = " ".join(query_parts)

    return {
        "is_opinion_inquiry": True,
        "target_subject": target_subject,
        "target_aspect": target_aspect,
        "opinion_keyword": found_opinion,
        "search_query": search_query,
        "is_comparison": is_comparison,
    }


_META_OR_CONFUSED_MEMORY_PATTERNS = (
    r"(?:具体的な意味や文脈は直近会話では不明|意味や文脈は不明|正体不明)",
    r"(?:についてAIに意見を求めている|についてAIに意見を聞いている)",
    r"(?:についてどう思うか質問した|について質問した|について聞いた)",
    r"(?:の意味が分からず|の意味を知らず)",
)


def is_meta_or_confused_memory(content: str) -> bool:
    """記憶が単なるユーザーの質問履歴やAIの困惑・不知といったメタ記憶であるかを判定する。"""
    return any(bool(re.search(pat, content)) for pat in _META_OR_CONFUSED_MEMORY_PATTERNS)


def is_person_like_memory(subject: str, memories: list[str]) -> bool:
    """記憶の内容から、対象（subject）が人間・サーバーメンバーとして行動しているかを判定する。"""
    if not subject or not memories:
        return False
    esc_subj = re.escape(subject)
    person_patterns = (
        rf"ユーザー（{esc_subj}）",
        rf"【ユーザー】{esc_subj}",
        rf"(?:😡|😊|😭|🤔|😄|✨)?\s*{esc_subj}(?:が|は|と|に|から|より)[^\n。]*?(?:持ってきた|持参|来た|行った|言った|話した|くれた|食べた|怒った|笑った|遊んだ|入室|退出|チャット|発言|送った|投稿)",
        rf"{esc_subj}(?:さん|くん|ちゃん|氏|先生)",
    )
    for mem in memories:
        if any(bool(re.search(pat, mem)) for pat in person_patterns):
            return True
    return False


async def check_memory_for_topic(
    store: MemoryStore | None,
    *,
    persona: str,
    target_subject: str,
    target_aspect: str = "",
    guild_id: int | None = None,
) -> list[str]:
    """
    ステップ1: 記憶DBから対象の話題（作品・パート等）に関する記憶・実感を検索する。
    該当する記憶があればテキスト行のリストを返し、なければ空リストを返す。
    実態記憶（エピソード・出来事）が存在する場合は質問履歴や困惑などのメタ記憶を除外して優先する。
    """
    if store is None or not target_subject:
        return []

    query = f"{target_subject} {target_aspect}".strip()
    try:
        # キーワード検索（FTS5 / LIKE）で記憶を広めに探索（最大10件）
        if hasattr(store, "search_memories_by_keyword"):
            records = await store.search_memories_by_keyword(
                query,
                persona=persona,
                guild_id=guild_id,
                limit=10,
            )
            raw_memories: list[str] = []
            for r in (records or []):
                content = str(r.get("content", "") or "").strip()
                if content and target_subject in content:
                    raw_memories.append(content)
            if raw_memories:
                # 実態記憶（エピソード・出来事・感情）とメタ記憶（質問履歴・困惑）を分類
                substantive_memories = [m for m in raw_memories if not is_meta_or_confused_memory(m)]
                meta_memories = [m for m in raw_memories if is_meta_or_confused_memory(m)]

                # 実態記憶があれば実態記憶を最優先（メタ記憶は除外）
                if substantive_memories:
                    final_memories = substantive_memories[:3]
                else:
                    final_memories = meta_memories[:3]

                log.info(
                    "ステップ1: 記憶DBから該当話題を思い出すことに成功 (substantive=%d, meta=%d): %r",
                    len(substantive_memories),
                    len(meta_memories),
                    final_memories,
                )
                return final_memories
    except Exception as e:
        log.warning("check_memory_for_topic failed: %r", e)

    return []


def build_unknown_topic_fallback_reply(
    user_text: str = "",
    *,
    target_subject: str = "",
    persona: str | None = None,
) -> str:
    """
    ステップ3: インターネット検索でも十分な情報が得られなかった（または不明な）場合に、
    機械的なエラー文ではなく、キャラクターの自然な口調で「知らない」「やったことがないから分からない」
    というニュアンスを返す。
    """
    subject = str(target_subject or "").strip()

    # 1. configからの動的解決を最優先
    from lib.config_utils import cfg
    if subject:
        template = str(cfg("TOPIC_INVESTIGATION_UNKNOWN_FALLBACK_TEMPLATE", "") or "").strip()
        if template:
            try:
                return template.format(subject=subject)
            except Exception:
                return template
    else:
        fallback = str(cfg("TOPIC_INVESTIGATION_UNKNOWN_FALLBACK", "") or "").strip()
        if fallback:
            return fallback

    # 2. 未設定時の中立な汎用フォールバック
    if subject:
        return f"『{subject}』については十分な情報が確認できませんでした。"
    return "そのあたりについては詳しく確認できませんでした。"


def build_investigation_unknown_reply(
    target_subject: str = "",
    persona: str | None = None,
) -> str:
    """build_unknown_topic_fallback_replyの別名・エイリアス。"""
    return build_unknown_topic_fallback_reply(target_subject=target_subject, persona=persona)
