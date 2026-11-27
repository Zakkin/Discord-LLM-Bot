"""固有名詞・前提・用語崩れ・安心断定グラウンディング、口調マーカー、相談/調査分類ヘルパー。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Sequence

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    filter_query_terms_by_slang,
)
from front.hutaba.monitor.img2chan_posting_parts.terms_and_media import (
    _HUTABA_CONSULTATION_RE,
    _HUTABA_CONSULTATION_TERM_STOPWORDS,
    _HUTABA_FILENAME_TOKEN_RE,
    _HUTABA_IMAGE_QUESTION_RE,
    _HUTABA_INTERROGATIVE_RE,
    _HUTABA_QUESTION_RE,
    _HUTABA_RHETORICAL_OPINION_RE,
    _HUTABA_RHETORICAL_TAG_RE,
    _HUTABA_TOPIC_STOPWORDS,
    _extract_hutaba_non_quote_body,
)


_PERSON_FULLNAME_RE = re.compile(r"[ァ-ヶー]{2,}・[ァ-ヶー]{2,}")
_KATAKANA_TOKEN_RE = re.compile(r"[ァ-ヶー]{2,}")
_PERSON_CONTEXT_PATTERN = re.compile(r"(?:の二番煎じ|みたいな|級の|選手|投手|打者|監督|コーチ)")

_COMMON_KATAKANA_NON_PERSON_WORDS = {
    "ワンチャン", "チャンス", "ピンチ", "リアル", "ガチ", "マジ", "ヤバい",
    "ネタ", "ノリ", "テンプレ", "パロディ", "オマージュ", "リスペクト",
    "スレ", "レス", "ボケ", "ツッコミ", "スタイル", "パターン", "タイプ",
    "レベル", "スペック", "データ", "コメント", "リアクション", "アドバイス",
    "メンタル", "フィジカル", "ポテンシャル", "タイミング", "アクション",
    "イメージ", "アイデア", "ニュアンス", "トラブル", "リスク", "メリット", "デメリット",
    "システム", "プログラム", "ツール", "アプリ", "サイト", "ネット", "オンライン",
    "ピッチャー", "バッター", "ランナー", "キャッチャー", "ルーキー", "ベテラン",
    "フォーム", "ポジション", "スタミナ", "パワー", "スピード", "コントロール",
    "ノーコン", "ストレート", "スライダー", "カーブ", "フォーク", "チェンジアップ",
    "シンカー", "シュート", "カット", "ツーシーム", "シーズン", "チーム", "リーグ",
    "ホームラン", "ヒット", "エラー", "イニング", "アウト", "セーフ", "ストライク", "ボール",
    "軟投派", "本格派", "アンダー", "サイド", "オーバースロー", "セットアップ", "クローザー",
    "ショート", "サード", "セカンド", "ファースト", "レフト", "センター", "ライト",
    "ビビ", "ビビり", "ドキドキ", "ワクワク", "モヤモヤ", "イライラ", "ハラハラ",
}


def find_ungrounded_person_names(
    comment: str,
    thread_terms: set[str],
    *,
    stopwords: set[str] | None = None,
    persona_names: set[str] | None = None,
) -> list[str]:
    """生成文から、スレッド語彙（thread_terms）にグラウンディングされていない人名・固有名詞を検出する。"""
    raw = _extract_hutaba_non_quote_body(comment)
    if not raw:
        return []

    stop_words = (stopwords or _HUTABA_TOPIC_STOPWORDS) | _COMMON_KATAKANA_NON_PERSON_WORDS
    p_names = persona_names or set()
    norm_terms = {unicodedata.normalize("NFKC", t).lower().replace("・", "") for t in thread_terms}
    raw_terms = set(thread_terms)

    violations: list[str] = []
    seen: set[str] = set()

    # パターンA: フルネーム（中黒連結名）
    for m in _PERSON_FULLNAME_RE.finditer(raw):
        full_name = m.group(0).strip()
        if full_name in raw_terms or unicodedata.normalize("NFKC", full_name).lower() in norm_terms:
            continue
        parts = full_name.split("・")
        if all(p in raw_terms or unicodedata.normalize("NFKC", p).lower() in norm_terms for p in parts):
            continue
        if full_name not in seen:
            seen.add(full_name)
            violations.append(full_name)

    # パターンB: 同一文内の人物文脈共起カタカナ語
    sentences = re.split(r"(?<=[。！？!?\n])", raw)
    for s in sentences:
        s_clean = s.strip()
        if not s_clean or not _PERSON_CONTEXT_PATTERN.search(s_clean):
            continue
        for km in _KATAKANA_TOKEN_RE.finditer(s_clean):
            token = km.group(0).strip()
            if len(token) < 2:
                continue
            if (
                token in raw_terms
                or unicodedata.normalize("NFKC", token).lower() in norm_terms
                or token in stop_words
                or token in p_names
            ):
                continue
            if any(token in v for v in violations):
                continue
            if token not in seen:
                seen.add(token)
                violations.append(token)

    return violations


def strip_ungrounded_person_sentences(
    text: str,
    ungrounded_names: Sequence[str],
) -> str:
    """違反した未グラウンディング人名を含む文を除去し、残りの本文を返す。"""
    raw = str(text or "").strip()
    if not raw or not ungrounded_names:
        return raw
    sentences = re.split(r"(?<=[。！？!?\n])", raw)
    kept = [s.strip() for s in sentences if s.strip() and not any(name in s for name in ungrounded_names)]
    res = "\n".join(kept).strip()
    res = re.sub(r"([。！？!?])+[、，]+", r"\1", res)
    return re.sub(r"^[、，\s]+", "", res)


# 相手の属性・経歴を前提にする表現
_BORROWED_PREMISE_PATTERNS = [
    re.compile(r"(\d+)年(やってる|やって|続けてる|続け|プレイしてる|プレイ|やっ)"),
    re.compile(r"(\d+)年目"),
    re.compile(r"ずっと(やってる|やって|プレイしてる|プレイ)"),
    re.compile(r"長年"),
]

# 安全性の問いかけパターン
_SAFETY_QUESTION_RE = re.compile(r"(?:大丈夫|安全|問題(?:ない|無い))(?:\?|？|かい|かな|ですか|か)")
# 無条件の安心表現パターン
_UNCONDITIONAL_REASSURANCE_RE = re.compile(r"(?:安心ですよ|安心だよ|大丈夫ですよ|大丈夫だよ|問題ありません|問題ないですよ)")
# 条件付け表現パターン（これらが同文内に含まれていれば助言として許容）
_CONDITIONAL_REASONING_RE = re.compile(r"(?:(?<!やってる)(?<!してる)(?<!使ってる)(?<!プレイしてる)(?<!遊んでる)なら|場合は|確認して|次第|限り|であれば)")


def find_borrowed_premise(comment_body: str, quoted_text: str) -> list[str]:
    """引用元レスに含まれていない相手の経歴・期間前提（「4年やってる」等）の借用を検出する。
    
    引用対象がない（自発書き込み）場合は検査対象外。スレ全体の他レスに存在していても、
    引用元レスに書かれていなければ違反とする。
    """
    raw = _extract_hutaba_non_quote_body(comment_body)
    if not raw or not quoted_text or not quoted_text.strip():
        return []

    quoted_clean = quoted_text.strip()
    violations: list[str] = []
    seen: set[str] = set()

    for pat in _BORROWED_PREMISE_PATTERNS:
        for m in pat.finditer(raw):
            phrase = m.group(0)
            # フレーズの核（数字＋年、またはフレーズ自体）が引用元レスにあるか判定
            core = m.group(1) + "年" if m.lastindex and m.lastindex >= 1 and m.group(1) and m.group(1).isdigit() else phrase
            if core not in quoted_clean:
                if phrase not in seen:
                    seen.add(phrase)
                    violations.append(phrase)

    return violations


def find_collapsed_terms(comment_body: str, frequent_terms: dict[str, int]) -> list[str]:
    """スレッドの定型カタカナ用語（5回以上出現）を中途半端に切断した用語崩れ（「クリーンイン」等）を検出する。"""
    raw = _extract_hutaba_non_quote_body(comment_body)
    if not raw or not frequent_terms:
        return []

    violations: list[str] = []
    seen: set[str] = set()

    # カタカナ語トークンの抽出（4文字以上を対象）
    for km in _KATAKANA_TOKEN_RE.finditer(raw):
        w = km.group(0).strip()
        if len(w) <= 3:
            continue
        # 定型用語 y の真の接頭辞であり、かつ w 自体が定型用語として登録されていないか照合
        for y in frequent_terms:
            if y.startswith(w) and w != y:
                # スレ内の完全一致出現回数がゼロ（定型用語の崩れ）の場合に違反
                if frequent_terms.get(w, 0) == 0:
                    violation_key = f"{w}（→ {y}）"
                    if violation_key not in seen:
                        seen.add(violation_key)
                        violations.append(violation_key)

    return violations


def find_unconditional_reassurance(comment_body: str, quoted_text: str) -> list[str]:
    """安全性が未確定な問いかけに対する無条件の安心断定（「安心ですよ」等）を検出する。
    
    同文内に条件付け表現（「〜なら」「〜を確認して」等）が含まれる場合は許容する。
    """
    raw = _extract_hutaba_non_quote_body(comment_body)
    if not raw or not quoted_text or not quoted_text.strip():
        return []

    if not _SAFETY_QUESTION_RE.search(quoted_text):
        return []

    violations: list[str] = []
    sentences = re.split(r"(?<=[。！？!?\n])", raw)
    for s in sentences:
        s_clean = s.strip()
        if not s_clean:
            continue
        m = _UNCONDITIONAL_REASSURANCE_RE.search(s_clean)
        if m:
            # 条件付けがあれば除外
            if not _CONDITIONAL_REASONING_RE.search(s_clean):
                violations.append(m.group(0))

    return violations


def strip_premise_grounding_sentences(text: str, violations: Sequence[str]) -> str:
    """前提違反・用語崩れ・安心断定を含む文を除去し、残りの本文を返す。"""
    raw = str(text or "").strip()
    if not raw or not violations:
        return raw

    # 「クリーンイン（→ クリーンインストール）」のような表記から抽出単語を取り出す
    keywords: list[str] = []
    for v in violations:
        if "（→" in v:
            keywords.append(v.split("（→")[0].strip())
        else:
            keywords.append(v.strip())

    sentences = re.split(r"(?<=[。！？!?\n])", raw)
    kept = [s.strip() for s in sentences if s.strip() and not any(kw in s for kw in keywords if kw)]
    res = "\n".join(kept).strip()
    res = re.sub(r"([。！？!?])+[、，]+", r"\1", res)
    return re.sub(r"^[、，\s]+", "", res)


def _resolve_persona_tone_markers(raw_val: Any) -> tuple[str, ...] | None:
    """cfg から取得した PERSONA_TONE_MARKERS を正規化する。
    
    str の場合はカンマ分割、list/tuple/set の場合は文字列要素を抽出。
    空または None の場合は None を返す。
    """
    if raw_val is None:
        return None
    if isinstance(raw_val, str):
        items = tuple(m.strip() for m in raw_val.split(",") if m.strip())
        return items if items else None
    if isinstance(raw_val, (list, tuple, set)):
        items = tuple(str(m).strip() for m in raw_val if str(m).strip())
        return items if items else None
    return None


def _persona_tone_example_clause(markers: Any) -> str:
    """口調再試行注記に差し込む語尾の例示を、解決済みマーカーの先頭から組み立てる。"""
    if markers:
        first = str(markers[0]).strip()
        if first:
            return f"（例: 〜{first}）"
    return ""


def _classify_hutaba_reply_for_research(text: str) -> dict[str, Any]:
    """レス本文を分類し、相談・意見募集（consultation）や質問（question）であれば解決策検索用クエリを返す。"""
    clean = re.sub(r"https?://\S+", " ", str(text or "")).strip()
    if not clean:
        return {"category": "general", "is_consultation": False, "is_image_question": False, "query": "", "terms": []}

    # 引用行を除いた実本文を優先評価
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    body_lines = [ln for ln in lines if not ln.startswith((">", "＞"))]
    eval_text = "\n".join(body_lines) if body_lines else clean

    # 著名人・第三者への修辞的疑問（大喜利・皮肉のフリ）は検索対象から除外
    # ※「これどう思う？」「みんなはどう思う？」等の通常相談を巻き込まないよう注意
    if _HUTABA_RHETORICAL_OPINION_RE.search(eval_text.strip()):
        return {"category": "general", "is_consultation": False, "is_image_question": False, "query": "", "terms": []}

    # 画像特定・キャラクター確認質問の検知（例: 「これ誰？」「何のキャラ？」「元ネタ何？」）
    # ※単なる「この子」「これ」等の指示語を含む通常質問（例: 「この子って通るんですか？」）を誤検知しないよう特定要求語を検証
    has_image_q_target = bool(re.search(
        r"(?:誰|だれ|何(?:の(?:キャラ|作品|アニメ|マンガ|漫画|ゲーム|シーン|画像)|これ|んだこれ|コレ)|元ネタ|元ねた|出典|画像検索)",
        eval_text,
    ))
    if _HUTABA_IMAGE_QUESTION_RE.search(eval_text) and has_image_q_target and (
        len(eval_text.strip()) <= 35
        or any(k in eval_text for k in ("元ネタ", "元ねた", "何のキャラ", "何の作品", "誰これ", "これ誰", "画像検索", "出典"))
    ):
        return {
            "category": "image_question",
            "is_consultation": False,
            "is_image_question": True,
            "query": eval_text[:30].strip(),
            "terms": [],
        }

    # 修辞的な共感確認（「なあわかるか？カヤ？」等）は感想の表明であり情報質問ではない。
    # 疑問語が無く、文末が共感確認タグで終わる場合は検索対象から除外する。
    if _HUTABA_RHETORICAL_TAG_RE.search(eval_text.strip()) and not _HUTABA_INTERROGATIVE_RE.search(eval_text):
        return {"category": "general", "is_consultation": False, "is_image_question": False, "query": "", "terms": []}

    is_consultation = bool(_HUTABA_CONSULTATION_RE.search(eval_text))
    is_question = is_consultation or bool(_HUTABA_QUESTION_RE.search(eval_text))
    if not is_question:
        return {"category": "general", "is_consultation": False, "is_image_question": False, "query": "", "terms": []}

    # トピック名詞（英数字・カタカナ・主要漢字2〜6文字）を抽出
    raw_tokens = re.findall(
        r"[A-Za-z][A-Za-z0-9+._/-]{1,20}|[\u30A0-\u30FFー]{2,16}|[\u4E00-\u9FFF]{2,6}",
        eval_text,
    )
    terms: list[str] = []
    seen: set[str] = set()
    for tok in raw_tokens:
        t = tok.strip("・ー_./-")
        if len(t) < 2 or _HUTABA_FILENAME_TOKEN_RE.match(t):
            continue
        t_low = t.lower()
        if t_low in seen or t in _HUTABA_CONSULTATION_TERM_STOPWORDS or t_low in _HUTABA_CONSULTATION_TERM_STOPWORDS:
            continue
        seen.add(t_low)
        terms.append(t)
        if len(terms) >= 4:
            break

    # 【追加】スラング辞書による検索クエリトークンのサニタイズ（接頭辞「フラ」「すみフラ」等の除外）
    terms = filter_query_terms_by_slang(terms)

    if not terms:
        return {"category": "general", "is_consultation": False, "is_image_question": False, "query": "", "terms": []}

    suffix: list[str] = []
    if is_consultation:
        if any(w in eval_text for w in ("やってない", "廃止", "終了", "なくなった", "代わり", "代替", "みたいな")):
            suffix.append("代替")
        suffix.extend(["対策", "方法"])
    else:
        suffix.append("方法")

    query_parts = terms[:4] + [s for s in suffix if s not in terms[:4]]
    return {
        "category": "consultation" if is_consultation else "question",
        "is_consultation": is_consultation,
        "is_image_question": False,
        "query": " ".join(query_parts[:6]).strip(),
        "terms": terms,
    }


def _find_hutaba_consultation_target_reply(
    replies: list[dict[str, Any]],
    *,
    own_nos: set[str] | None = None,
    own_texts: set[str] | None = None,
) -> dict[str, Any] | None:
    """スレッド内の直近レスから相談・意見募集を行っているレスを末尾（新しい順）から探す。"""
    nos = own_nos or set()
    texts = own_texts or set()
    valid_replies = [r for r in replies if isinstance(r, dict)]
    for r in reversed(valid_replies[-25:]):
        p_no = str(r.get("post_no") or r.get("number") or "").strip()
        p_text = str(r.get("text") or "").strip()
        if not p_text:
            continue
        # 短文であっても画像特定質問ならチェック対象とする
        if len(p_text) < 10 and not _HUTABA_IMAGE_QUESTION_RE.search(p_text):
            continue
        if p_no and p_no in nos:
            continue
        if p_text in texts:
            continue
        info = _classify_hutaba_reply_for_research(p_text)
        if info.get("is_consultation") or info.get("is_image_question"):
            return r
    return None
