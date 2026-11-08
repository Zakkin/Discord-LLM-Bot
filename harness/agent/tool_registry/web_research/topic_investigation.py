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
    r"(?:クソゲー|クソアニメ|クソ映画|クソ仕様|クソ回|クソ(?:だ|で|な|すぎ|じゃ|かよ)|微妙|重い|重さ|重くて|退屈|神ゲー|名作|傑作|良作|駄作)|"
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

# 暴言・罵倒・感情攻撃を表すキーワードパターン（評価問い合わせではない文脈）
_ABUSE_OR_INSULT_PATTERN = re.compile(
    r"(?:"
    r"黙れ|"
    r"死ね|死んどけ|くたばれ|消えろ|失せろ|"
    r"カス|ゴミ|ボケ|"
    r"クソブス|クソ野郎|クソバカ|クソゴミ|"
    r"調子[に]?乗|"
    r"手を出さねえ|ぶっ[殺ころ]"
    r")",
    re.IGNORECASE,
)

# シミュレーション・影響検証を表すキーワードパターン（客観的事実検証・ファクトチェック対象であり評価問い合わせではない文脈）
_SIMULATION_IMPACT_PATTERN = re.compile(
    r"(?:"
    r"(?:食らっ|くらっ|喰らっ|浴び|あび|受け|うけ|食べ|たべ|当たっ|あたっ|触っ|さわっ|刺され|さされ|刺し|さし|落ち|おち|吸っ|すっ|漏れ|もれ|流れ|ながれ|噛まれ|かまれ|撃たれ|うたれ|撃ち|し|され|なっ|起き|おき)たら"
    r"|(?:飲ん|のん|踏ん|ふん|噛ん|かん|死ん|しん)だら"
    r"|(?:する|受ける|浴びる|食らう|喰らう|触る|飲む|食べる|落ちる|吸う|流れる|起きる|噛む|踏む)と"
    r")"
    r"(?:[^\n。！？!?]{0,25}?)"
    r"(?:どうなる|どうなるの|どうなるのか|どうなりますか|何が起きる|何が起こる|死ぬの|死ぬのか|死にますか|危険なの|大丈夫なの|耐えられるの|影響は|影響ある)"
    r"|(?:人体への影響|人体影響|健康被害|致死量|致死電流|致死電圧|悪影響|後遺症)",
    re.IGNORECASE,
)

# 主題として不自然な動詞命令形・文の断片パターン
_INVALID_SUBJECT_VERB_IMPERATIVE = re.compile(
    r"(?:黙れ|死ね|死んどけ|くたばれ|消えろ|失せろ|やめろ|行け|見ろ|待て|乗れ|出せ|言え)",
    re.IGNORECASE,
)
_INVALID_SUBJECT_PARTICLES_OR_PRONOUNS = re.compile(
    r"(?:お前|あんた|てめえ|貴様|マジで|ガチで|じゃね[ええ]|だろ|って[んの]|んだろ|調子乗)",
    re.IGNORECASE,
)

# 動詞語幹（乗っ、行っ、言っ、待っ、取っ、買っ等）に続く促音便「って」を助詞「って」と誤認しないための否定戻り読み
# ※Pythonのreは固定幅のlookbehindのみ許可するため、1文字の文字クラスで指定する
_VERB_TE_FORM_LOOKBEHIND = (
    r"(?<![乗行言待取買勝知使笑立持走降送終作変入出頼思分打撃追貼張狂怒当集通触配払拾渡吸洗雇失奪断黙去止泊眠残重のいまとかしもっ])って"
)


def is_plausible_topic_subject(subject: str) -> bool:
    """
    抽出された主題が作品名・事物名として妥当かを検証する多重防御安全弁。
    暴言、命令形、過剰な記号、不自然な空白、長すぎる文の断片等を除外する。
    """
    s = str(subject or "").strip()
    if not s or len(s) < 2 or len(s) > 30:
        return False

    # 暴言・罵倒が含まれる場合は除外
    if _ABUSE_OR_INSULT_PATTERN.search(s):
        return False

    # 句読点（。、！？など）や改行が含まれる場合は文の断片とみなす
    if re.search(r"[。、！？!?…‥\n]", s):
        return False

    # 記号が過剰に含まれる（2文字以上の記号連続等）
    if re.search(r"[-_・/／~〜=+\*#@&%$!?:;]{2,}", s):
        return False

    # 全角空白、または日本語平仮名・漢字に挟まれた空白等の不自然な空白
    if "\u3000" in s or re.search(r"[\u3040-\u309F]\s+[\u3040-\u309F\u4E00-\u9FFF]", s):
        return False

    # 命令形や人称代名詞・口語スラングが含まれる場合は除外
    if _INVALID_SUBJECT_VERB_IMPERATIVE.search(s):
        return False
    if _INVALID_SUBJECT_PARTICLES_OR_PRONOUNS.search(s):
        return False

    # 10文字以上で助詞等の文の断片を含む場合は除外
    if len(s) >= 10:
        if re.search(r"(?:でお前|はお前|でおれ|はおれ|じゃねえ)", s):
            return False

    # 助詞や接続助詞・終助詞で終わる場合は文の断片として除外
    if re.search(r"(?:は|が|で|を|に|と|から|より|へ|って|けど|けれど|のに|ぞ|ぜ|よ|ね)$", s):
        return False

    return True


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

    # 暴言・罵倒・感情攻撃文脈であれば評価問い合わせではない
    if _ABUSE_OR_INSULT_PATTERN.search(trimmed):
        return {
            "is_opinion_inquiry": False,
            "target_subject": "",
            "target_aspect": "",
            "opinion_keyword": "",
            "search_query": "",
            "is_comparison": False,
        }

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

    # シミュレーション・人体影響検証などの客観的検証文脈であれば評価問い合わせではない
    if _SIMULATION_IMPACT_PATTERN.search(trimmed):
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
        before_comp = re.sub(
            rf"(?:の中で|なら|だと|で|は|{_VERB_TE_FORM_LOOKBEHIND}|、)+$",
            "",
            before_comp,
        ).strip()
        m_comp_subj = re.search(
            r"([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_、／/\s]{2,40})$",
            before_comp,
        )
        if m_comp_subj:
            cand = m_comp_subj.group(1).strip()
            if cand != target_aspect and is_plausible_topic_subject(cand):
                target_subject = cand
                is_comparison = True

    # 2. 比較から取れなかった場合：助詞（は、って、の、における、について、に関して）または読点（、）で区切られた先頭の名詞句
    if not target_subject:
        subject_match = re.match(
            rf"^(?:あの|その|この)?([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_]{{2,30}}?)(?:(?:は|の|{_VERB_TE_FORM_LOOKBEHIND}|、)|における|について(?:は)?|に関して(?:は)?|\s)",
            trimmed,
        )
        if subject_match:
            cand = subject_match.group(1).strip()
            if cand != target_aspect and is_plausible_topic_subject(cand):
                target_subject = cand

    # 3. 文頭から取れなかった場合、観点の前の名詞句を探す
    if not target_subject and target_aspect:
        before_aspect = trimmed.split(target_aspect, 1)[0].strip()
        m_before = re.search(
            rf"([A-Za-z0-9\u30A1-\u30FA\u3041-\u3096\u4E00-\u9FFFー・_]{{2,30}}?)(?:(?:は|の|{_VERB_TE_FORM_LOOKBEHIND}|、|\s)|について|に関して)*$",
            before_aspect,
        )
        if m_before:
            cand = m_before.group(1).strip()
            if cand != target_aspect and is_plausible_topic_subject(cand):
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


_CONSULTATION_TROUBLE_RE = re.compile(
    r"(?:どうすりゃいい|どうすればいい|どうしたらいい|どうすりゃ|どうしよ|"
    r"途方に暮れ|困って(?:る|いる|ます|おり)|悩んで(?:る|いる|ます)|"
    r"詰んで(?:る|いる)|手詰まり|うまくいかない|上手くいかない|"
    r"できなくて|出来なくて|なくなって|やめてる(?:所|ところ|サイト)が多|"
    r"廃止され|終了してしまって)",
    re.IGNORECASE,
)

_CONSULTATION_SEEKING_RE = re.compile(
    r"(?:してる人(?:いない|おらん|いる)|やってる人(?:いない|おらん|いる)|"
    r"使ってる人(?:いない|おらん|いる)|知ってる人(?:いない|おらん|いる)|"
    r"詳しい人(?:いない|おらん|いる)|"
    r"聞きたい(?:けど|んだが|んだけど|んですが|のだけど)|"
    r"意見(?:を|が)?(?:聞きたい|ほしい|欲しい|求む)|"
    r"(?:いい|良い|うまい|上手い|代わりの|代替の)?(?:方法|やり方|手段|ソフト|ツール|アプリ|サービス|対策|解決策)(?:ない|ある|教えて|知りません)|"
    r"みんなどう(?:してる|やってる)|どうやって(?:る|ますか)|"
    r"どうすれば|どうすりゃ|どうしたら|おすすめ(?:の|な)?(?:ソフト|ツール|アプリ|方法|やり方))",
    re.IGNORECASE,
)

_CONSULTATION_NOISE_TERMS = {
    "人", "方", "多く", "多い", "昔", "今", "最近", "自分", "俺", "私", "僕",
    "これ", "それ", "あれ", "どれ", "ここ", "そこ", "どこ", "ため", "こと",
    "もの", "とき", "時", "場合", "感じ", "みたい", "よう", "そう", "どう",
    "途方", "意見", "質問", "相談", "返信", "レス", "スレ", "スレッド",
    "みんな", "誰か", "本当", "マジ", "時間", "無限", "今日", "昨日", "明日",
}

_IMAGE_FILENAME_NOISE_RE = re.compile(
    r"^(?:fu?\d+|\d+)(?:\.(?:jpg|jpeg|png|gif|webp|mp4|webm))?$",
    re.IGNORECASE,
)

_NOT_PLAUSIBLE_TOPIC_SUBJECTS = {
    "お前", "あんた", "てめえ", "貴様", "マジで", "ガチで", "だろ", "んだろ", "調子乗",
}

_ABUSIVE_OR_NOISE_SUBSTR_RE = re.compile(
    r"(?:黙れ|死ね|死んどけ|くたばれ|消えろ|失せろ|カス|ゴミ|ボケ|クソ|ぶっ[殺ころ])",
    re.IGNORECASE,
)


def detect_consultation_or_problem_solving_inquiry(
    user_text: str,
    context: str = "",
) -> dict[str, Any]:
    """困りごと・疑問・意見募集・代替手段の模索レスであるかを判定し、解決策検索用クエリを構築する。"""
    empty_res: dict[str, Any] = {
        "is_consultation_inquiry": False,
        "problem_keywords": [],
        "consultation_marker": "",
        "search_query": "",
    }
    raw = str(user_text or "").strip()
    if not raw or len(raw) < 8:
        return empty_res

    clean = re.sub(r"https?://\S+", " ", raw)
    clean = re.sub(r"<@!?\d+>", " ", clean)
    clean = re.sub(r"^[（(][^）)]*?への返信[）)]\s*", "", clean, flags=re.DOTALL).strip()
    # 引用行（>〜）と本文の両方を評価対象にしつつ、本文側にも相談マーカーがあるか確認
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    body_lines = [ln for ln in lines if not ln.startswith((">", "＞"))]
    eval_text = "\n".join(body_lines) if body_lines else clean

    trouble_m = _CONSULTATION_TROUBLE_RE.search(eval_text)
    seeking_m = _CONSULTATION_SEEKING_RE.search(eval_text)

    if not trouble_m and not seeking_m:
        return empty_res

    # 単なる体調・感情独白（「眠くてどうしよう」「疲れて途方に暮れてる」等で具体名詞がないもの）を除外
    has_concrete_consultation = bool(
        (trouble_m and seeking_m)
        or (seeking_m and ("?" in eval_text or "？" in eval_text or "聞きたい" in eval_text or "いない" in eval_text or "おらん" in eval_text or "方法" in eval_text or "代替" in eval_text))
        or (trouble_m and any(k in eval_text for k in ("調べたら", "サイト", "ソフト", "アプリ", "ツール", "設定", "エラー", "更新", "フィード", "自作", "常駐", "動かない", "使えない")))
    )
    if not has_concrete_consultation:
        return empty_res

    marker = (seeking_m.group(0) if seeking_m else "") or (trouble_m.group(0) if trouble_m else "")

    # 技術名詞・固有名詞・重要キーワードの抽出（英数字、カタカナ、漢字2〜6文字）
    combined_for_terms = clean
    raw_tokens = re.findall(
        r"[A-Za-z][A-Za-z0-9+._/-]{1,20}|[\u30A0-\u30FFー]{2,16}|[\u4E00-\u9FFF]{2,6}",
        combined_for_terms,
    )
    keywords: list[str] = []
    seen_low: set[str] = set()
    for tok in raw_tokens:
        t = tok.strip("・ー_./-")
        if len(t) < 2:
            continue
        t_low = t.lower()
        if t_low in seen_low or t in _CONSULTATION_NOISE_TERMS or t in _NOT_PLAUSIBLE_TOPIC_SUBJECTS:
            continue
        if _IMAGE_FILENAME_NOISE_RE.match(t):
            continue
        if _ABUSIVE_OR_NOISE_SUBSTR_RE.search(t):
            continue
        seen_low.add(t_low)
        keywords.append(t)
        if len(keywords) >= 5:
            break

    if not keywords:
        return empty_res

    # 解決策・代替案を引くための検索クエリ構築
    suffix_terms: list[str] = []
    if any(w in clean for w in ("やってない", "廃止", "終了", "なくなった", "代わり", "代替", "みたいな")):
        suffix_terms.append("代替")
    if any(w in clean for w in ("自作", "常駐", "スクリプト", "ツール", "ソフト")):
        if "方法" not in keywords:
            suffix_terms.append("方法")
    else:
        suffix_terms.extend(["対策", "方法"])

    query_tokens = keywords[:4] + [s for s in suffix_terms if s not in keywords[:4]]
    search_query = " ".join(query_tokens[:6]).strip()

    return {
        "is_consultation_inquiry": True,
        "problem_keywords": keywords,
        "consultation_marker": marker,
        "search_query": search_query,
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
    if subject and not is_plausible_topic_subject(subject):
        subject = ""

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
