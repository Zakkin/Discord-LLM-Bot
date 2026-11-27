"""スレッド用語抽出・メディア判定・周辺レス添付画像コンテキスト収集ヘルパー。"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from front.hutaba.monitor.img2chan_posting_parts.deps import (
    _normalize_img_post_no,
    cfg,
    extract_named_term_candidates,
)


_BRACKETED_TERM_RE = re.compile(r"[「『]([^」』\n]{2,20})[」』]")
_KATAKANA_TERM_RE = re.compile(r"[ァ-ヶー]{3,16}")
_ALPHANUM_TERM_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{2,15}\b")

_HUTABA_TERM_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("NAI", "novelai", "NovelAI", "ノベルAI"),
    ("LoRA", "lora", "Lola", "lola", "ローラ"),
    ("ComfyUI", "comfyui", "Comfy"),
    ("StableDiffusion", "stablediffusion", "SD"),
    ("PixAI", "pixai", "ピクスAI"),
)
_HUTABA_TERM_SYNONYM_MAP: dict[str, tuple[str, ...]] = {
    term.lower(): group
    for group in _HUTABA_TERM_SYNONYM_GROUPS
    for term in group
}

_HUTABA_TOPIC_STOPWORDS = {
    "としあき", "スレッド", "レス", "そうだね", "カタログ", "シャンクス",
    "オッス", "ありがとう", "どうして", "なんや",
    "http", "https", "net", "com", "htm", "html", "ちゃんねる",
    "jpg", "jpeg", "png", "gif", "webp", "mp4", "webm",
    "これ", "それ", "あれ", "ここ", "そこ", "あそこ", "どれ", "なに", "なん", "ため", "よう", "こと", "もの", "とこ",
    "ちゃん", "さん", "くん", "たち", "みたい", "感じ", "場合", "結果", "理由", "意味", "話題", "最近", "今日", "昨日",
    "明日", "自分", "相手", "人間", "世界", "日本", "普通", "本当", "マジ", "やばい", "すごい", "多い", "少ない",
    "スレ", "画像", "動画", "記事", "ニュース", "情報", "界隈", "名前", "部分", "状態", "関係", "問題",
    "キャラ", "キャラクター", "アニメ", "マンガ", "漫画", "ゲーム", "作品", "原作", "先生", "教師", "生徒",
    "性的", "エロ", "モザイク", "ファン", "オタク", "シーン", "セリフ", "設定", "展開", "最終回", "最新話",
    # 検索ノイズ防止用ストップワード追加
    "無理", "面白", "面白い", "所他", "刺客", "警戒", "結局", "今更", "結局何", "怯え",
    "困る", "困り", "困った", "デカパイ", "巨乳", "美乳", "爆乳", "おっぱい",
}
_HUTABA_STOPWORDS = _HUTABA_TOPIC_STOPWORDS

_HUTABA_FILENAME_TOKEN_RE = re.compile(
    r"^(?:fu?\d+|\d+|[a-z0-9_]+)\.(?:jpg|jpeg|png|gif|webp|mp4|webm)$|^fu\d{4,}$|^(?:jpg|jpeg|png|gif|webp|mp4|webm)$",
    re.IGNORECASE,
)

_HUTABA_CONSULTATION_RE = re.compile(
    r"(?:してる人(?:いない|おらん|いる)|やってる人(?:いない|おらん|いる)|"
    r"使ってる人(?:いない|おらん|いる)|知ってる人(?:いない|おらん|いる)|"
    r"聞きたい(?:けど|んだが|んだけど|んですが)|"
    r"どうすりゃいい|どうすればいい|どうしたらいい|途方に暮れ|"
    r"困って(?:る|いる|ます)|悩んで(?:る|いる)|"
    r"(?:いい|良い|代わりの|代替の)?(?:方法|やり方|手段|ソフト|ツール|サイト|対策)(?:ない|ある|教えて)|"
    r"みんなどう(?:してる|やってる)|おすすめ(?:の|な)?(?:ソフト|ツール|方法))",
    re.IGNORECASE,
)

_HUTABA_QUESTION_RE = re.compile(
    r"[?？]|(?:教えて|知ってる|どうすれば|どうやって|なぜ|なんで|どうして|何使ってる)",
    re.IGNORECASE,
)

# 画像特定・キャラクター質問検知用正規表現（「これ誰」「誰これ」「何のキャラ」「元ネタ」等）
_HUTABA_IMAGE_QUESTION_RE = re.compile(
    r"(?:これ|この(?:人|キャラ|画像|男|女|子|おっさん|おじさん)?|誰|だれ|何(?:の(?:キャラ|作品|アニメ|マンガ|漫画|ゲーム|シーン|画像)|これ|んだこれ|コレ)|元ネタ|元ねた|出典|画像検索)",
    re.IGNORECASE,
)

# 著名人・第三者への修辞的疑問（大喜利・皮肉のフリ）は検索対象から除外
# ※「これどう思う？」「みんなはどう思う？」等の通常相談を巻き込まないよう注意
_HUTABA_RHETORICAL_OPINION_RE = re.compile(
    r"(?:(?<!これ)(?<!それ)(?<!あれ)(?<!みんな)(?<!「」)(?<!としあき)(?<!俺)(?<!自分)(?<!私)(?<!ワイ)(?<!ぼく)(?<!僕)[はって]\s*(?:正直\s*)?(?:どう思う|どう考える|どうすんだ(?:ろ)?|どうする気)|の気持ち|どういう気持ち|今頃何して|笑ってそう)[?？!！…・]*$",
    re.IGNORECASE,
)

# 修辞的な共感確認・同意求め（「なあわかるか？」「〜だよね？」等）は情報質問ではない
_HUTABA_RHETORICAL_TAG_RE = re.compile(
    r"(?:わかる|分かる)か[？?](?:[^\n？?]{0,6}[？?])?\s*$|(?:だろ|でしょ|じゃない|じゃね|だよね|だよな)[？?]\s*$",
    re.IGNORECASE,
)

# 情報を求める疑問語（これらがある場合は修辞的タグがあっても質問として扱う）
_HUTABA_INTERROGATIVE_RE = re.compile(
    r"(?:何|誰|どこ|いつ|なぜ|なんで|どうして|どう|どれ|どの|どんな|いくつ|いくら)",
    re.IGNORECASE,
)

_HUTABA_CONSULTATION_TERM_STOPWORDS = _HUTABA_TOPIC_STOPWORDS | {
    "無限", "時間", "自分", "最近", "今日", "昨日", "明日", "途方", "意見",
    "質問", "相談", "サイト", "多く", "多い", "調べ", "調べたら", "聞きたい",
    "常駐",
}

_HUTABA_VIDEO_MEDIA_RE = re.compile(
    r"(?:fu?\d+|[a-zA-Z0-9_-]+)\.(?:mp4|webm|avi|mov|mkv|flv)\b|https?://(?:dec\.)?2chan\.net/(?:up|up2)/src/fu?\d+\.(?:mp4|webm|avi|mov|mkv|flv)",
    re.IGNORECASE,
)

_HUTABA_ATTACHED_IMAGE_RE = re.compile(
    r"\b(fu\d+)\.(jpg|jpeg|png|gif|webp)\b|\b(f\d+)\.(jpg|jpeg|png|gif|webp)\b|https?://(?:dec\.)?2chan\.net/(?:up|up2)/src/(fu?\d+\.(?:jpg|jpeg|png|gif|webp))",
    re.IGNORECASE,
)


def _has_hutaba_unsupported_media(text: str) -> bool:
    """レス本文に動画ファイル（mp4, webm等）が含まれており、AIモデルが直接読み取れないか判定する。"""
    raw = str(text or "").strip()
    return bool(_HUTABA_VIDEO_MEDIA_RE.search(raw))


def _extract_hutaba_attached_image_urls(text: str) -> list[str]:
    """レス本文からあぷ小・あぷの画像URLを抽出して返す。"""
    raw = str(text or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for m in _HUTABA_ATTACHED_IMAGE_RE.finditer(raw):
        # 1. 直接URLマッチ
        if m.group(0).startswith("http"):
            u = m.group(0)
            if u not in seen:
                seen.add(u)
                urls.append(u)
            continue
        # 2. あぷ小 (fu12345.ext)
        if m.group(1):
            fn = f"{m.group(1)}.{m.group(2)}"
            u = f"https://dec.2chan.net/up2/src/{fn}"
            if u not in seen:
                seen.add(u)
                urls.append(u)
            continue
        # 3. あぷ (f12345.ext)
        if m.group(3):
            fn = f"{m.group(3)}.{m.group(4)}"
            u = f"https://dec.2chan.net/up/src/{fn}"
            if u not in seen:
                seen.add(u)
                urls.append(u)
            continue
    return urls


_HUTABA_SHORT_POST_NO_RE = re.compile(r"^(?:No\.?)?(\d+)$", re.IGNORECASE)
_HUTABA_LINE_POST_NO_RE = re.compile(r"^(?:>>\d+\s+)?No\.(\d+)\b", re.IGNORECASE)
_HUTABA_CONTEXT_RECENT_REPLY_LIMIT = 10


def _extract_hutaba_post_no_key(post: object) -> str:
    """レス辞書等から比較・キャッシュ用の投稿番号キーを取り出す。"""
    if not isinstance(post, dict):
        return ""
    raw = str(post.get("post_no") or "").strip()
    if not raw:
        return ""
    norm = _normalize_img_post_no(raw)
    if norm:
        return norm
    m = _HUTABA_SHORT_POST_NO_RE.match(raw)
    if m:
        return m.group(1)
    return raw


def _is_hutaba_context_image_caption_enabled() -> bool:
    """周辺レスの添付画像キャプション注入が有効かどうかを返す（既定 True）。"""
    raw = cfg("IMG2CHAN_CONTEXT_IMAGE_CAPTION_ENABLED", True)
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    norm = str(raw).strip().lower()
    if not norm:
        return True
    if norm in {"0", "false", "no", "off"}:
        return False
    if norm in {"1", "true", "yes", "on"}:
        return True
    return bool(raw)


def _resolve_hutaba_caption_fn(helper: object):
    """caption_hutaba_thread_image 呼び出し関数を解決する（helper 優先、直接 import フォールバック）。"""
    caption_fn = getattr(helper, "caption_hutaba_thread_image", None)
    if callable(caption_fn):
        return caption_fn
    try:
        from front.hutaba.client.img2chan import caption_hutaba_thread_image as _caption_direct
        if callable(_caption_direct):
            return _caption_direct
    except Exception:
        pass
    return None


def _collect_hutaba_prompt_context_replies(
    *,
    thread: dict,
    posts: list,
    target_reply: object,
    early_lines: object,
    target_context_lines: object,
    helper: object,
) -> list:
    """プロンプトに含まれる周辺レス群（OP・対象レス除外）を新しい順に返す。"""
    thread_dict = thread if isinstance(thread, dict) else {}
    post_list = [p for p in (posts or []) if isinstance(p, dict)]

    op_post_no = _extract_hutaba_post_no_key(
        {"post_no": thread_dict.get("op_post_no") or thread_dict.get("thread_no") or ""}
    )
    if not op_post_no and post_list and bool(post_list[0].get("is_op")):
        op_post_no = _extract_hutaba_post_no_key(post_list[0])

    target_dict = target_reply if isinstance(target_reply, dict) else None
    target_post_no = _extract_hutaba_post_no_key(target_dict)
    target_text = str(target_dict.get("text") or "").strip() if target_dict else ""
    try:
        target_number = int(target_dict.get("number") or 0) if target_dict else 0
    except Exception:
        target_number = 0

    raw_replies = thread_dict.get("replies")
    replies_list = [r for r in raw_replies if isinstance(r, dict)] if isinstance(raw_replies, list) else []
    if not replies_list and post_list:
        replies_list = [
            p
            for p in post_list
            if not bool(p.get("is_op"))
            and (not op_post_no or _extract_hutaba_post_no_key(p) != op_post_no)
        ]

    posts_by_no: dict = {}
    order_by_no: dict = {}
    order_idx = 0
    for item in post_list + replies_list:
        p_no = _extract_hutaba_post_no_key(item)
        if not p_no:
            continue
        posts_by_no.setdefault(p_no, item)
        if p_no not in order_by_no:
            order_by_no[p_no] = order_idx
            order_idx += 1

    def _is_excluded(cand: dict) -> bool:
        if bool(cand.get("is_op")):
            return True
        p_no = _extract_hutaba_post_no_key(cand)
        if not p_no:
            return True
        if op_post_no and p_no == op_post_no:
            return True
        if target_dict is not None:
            if cand is target_dict:
                return True
            if target_post_no and p_no == target_post_no:
                return True
            if not target_post_no:
                try:
                    cand_num = int(cand.get("number") or 0)
                except Exception:
                    cand_num = 0
                if target_number > 0 and cand_num == target_number:
                    return True
                cand_text = str(cand.get("text") or "").strip()
                if target_text and cand_text == target_text:
                    return True
        return False

    candidates_by_no: dict = {}

    def _add_candidate(cand: object) -> None:
        if not isinstance(cand, dict):
            return
        if _is_excluded(cand):
            return
        p_no = _extract_hutaba_post_no_key(cand)
        canonical = posts_by_no.get(p_no, cand)
        if _is_excluded(canonical):
            return
        candidates_by_no.setdefault(p_no, canonical)

    recent_replies = replies_list[-_HUTABA_CONTEXT_RECENT_REPLY_LIMIT:]
    for r in recent_replies:
        _add_candidate(r)

    recent_nos = {_extract_hutaba_post_no_key(r) for r in recent_replies if _extract_hutaba_post_no_key(r)}
    if len(replies_list) > _HUTABA_CONTEXT_RECENT_REPLY_LIMIT:
        calc_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        find_fn = getattr(helper, "find_img_thread_quoted_posts", None)
        if callable(calc_fn) and callable(find_fn):
            try:
                reply_counts = calc_fn(replies_list)
                if isinstance(reply_counts, dict):
                    added_hot = 0
                    for r in recent_replies:
                        if added_hot >= 3:
                            break
                        parents = find_fn(replies_list, r)
                        if not isinstance(parents, (list, tuple)):
                            continue
                        for parent in parents:
                            if not isinstance(parent, dict):
                                continue
                            parent_no = _extract_hutaba_post_no_key(parent)
                            if (
                                parent_no
                                and parent_no not in recent_nos
                                and parent_no not in candidates_by_no
                                and int(reply_counts.get(parent_no, 0) or 0) > 0
                            ):
                                _add_candidate(parent)
                                added_hot += 1
                                if added_hot >= 3:
                                    break
            except Exception:
                pass

    if target_dict is not None:
        chain_fn = getattr(helper, "build_img_thread_reply_context_chain", None)
        if callable(chain_fn):
            try:
                chain = chain_fn(thread_dict, target_dict, max_depth=3)
                if isinstance(chain, (list, tuple)):
                    for item in chain:
                        _add_candidate(item)
            except Exception:
                pass

    for line_list in (early_lines, target_context_lines):
        if not isinstance(line_list, (list, tuple)):
            continue
        for line in line_list:
            m = _HUTABA_LINE_POST_NO_RE.match(str(line or "").strip())
            if not m:
                continue
            p_no = m.group(1)
            if p_no in posts_by_no:
                _add_candidate(posts_by_no[p_no])

    def _sort_key(cand: dict) -> tuple:
        p_no = _extract_hutaba_post_no_key(cand)
        ord_i = order_by_no.get(p_no, -1)
        try:
            num = int(cand.get("number") or 0)
        except Exception:
            num = 0
        p_int = int(p_no) if p_no.isdigit() else 0
        return (ord_i, num, p_int)

    return sorted(candidates_by_no.values(), key=_sort_key, reverse=True)


def _drop_phrase_component_tokens(terms: list[str]) -> list[str]:
    """空白を含むフレーズの構成トークンと大小文字無視で完全一致する単独トークン語を除去する。"""
    component_tokens: set[str] = set()
    for term in terms:
        parts = term.split()
        if len(parts) > 1:
            for part in parts:
                component_tokens.add(part.lower())
    if not component_tokens:
        return list(terms)
    return [
        term
        for term in terms
        if len(term.split()) > 1 or term.strip().lower() not in component_tokens
    ]


def _extract_hutaba_named_terms(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    terms: list[str] = []
    if extract_named_term_candidates is not None:
        try:
            terms.extend(extract_named_term_candidates(raw))
        except Exception:
            pass
    for m in _BRACKETED_TERM_RE.finditer(raw):
        val = m.group(1).strip()
        if val and len(val) >= 2 and val not in _HUTABA_TOPIC_STOPWORDS:
            terms.append(val)
    for m in _KATAKANA_TERM_RE.finditer(raw):
        val = m.group(0).strip()
        if val and len(val) >= 3 and val not in _HUTABA_TOPIC_STOPWORDS:
            terms.append(val)
    for m in _ALPHANUM_TERM_RE.finditer(raw):
        val = m.group(0).strip()
        if val and len(val) >= 3 and val.lower() not in _HUTABA_TOPIC_STOPWORDS:
            terms.append(val)
    seen: set[str] = set()
    deduped: list[str] = []
    for t in terms:
        if _HUTABA_FILENAME_TOKEN_RE.match(t):
            continue
        k = t.lower()
        if k in seen or k in _HUTABA_TOPIC_STOPWORDS:
            continue
        seen.add(k)
        deduped.append(t)

    # 略称・表記揺れ同義語の展開（例: "novelai" <-> "NAI", "Lola" <-> "LoRA"）
    raw_lower = unicodedata.normalize("NFKC", raw).lower()
    for group in _HUTABA_TERM_SYNONYM_GROUPS:
        if any(g.lower() in seen or re.search(rf"(?<![a-z0-9]){re.escape(g.lower())}(?![a-z0-9])", raw_lower) for g in group):
            for g in group:
                gk = g.lower()
                if gk not in seen and gk not in _HUTABA_TOPIC_STOPWORDS:
                    seen.add(gk)
                    deduped.append(g)
    # 後段抽出パス（_ALPHANUM_TERM_RE 等）からの構成トークン単独再混入を _drop_phrase_component_tokens で除去する
    deduped = _drop_phrase_component_tokens(deduped)
    return deduped


def _extract_hutaba_non_quote_body(text: Any) -> str:
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.strip().startswith(">")
    ]
    return "\n".join(lines).strip()


def _strip_hutaba_quote_lines(text: Any) -> str:
    return _extract_hutaba_non_quote_body(text)
