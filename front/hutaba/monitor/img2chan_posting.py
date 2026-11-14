"""img.2chan.netへの投稿生成、投稿実行、スレ監視ループを担当するMixin。
DiscordメッセージからimgスレURLを拾い、遅延投稿・返信追跡・結果報告までを行います。
"""
from __future__ import annotations

import asyncio
from collections import Counter
import contextlib
import random
import re
import time
import unicodedata
from typing import Any, Iterable, Sequence

import discord

from ollama_bot.common.discord_helpers import author_id, channel_id, content
from harness.agent.memory_manager.logic import get_relevant_memories, get_user_habit_profile_lines
from ollama_bot.common.ollama_helpers import extract_first_user_facing_reply, looks_like_abnormal_assistant_reply, looks_like_parrot_reply, sanitize_generated_reply, truncate_text
from ollama_bot.ollama_chat_.ollama_chat_helpers import _get_channel_kind
from front.hutaba.monitor.img2chan_context import _get_img2chan_helper, cfg, cfg_bool, cfg_float, cfg_int, log
from front.hutaba.utils import _IMG_THREAD_URL_PATTERN, normalize_img_thread_url
from lib.text_utils import compact_exception_message as _compact_exception_message

try:
    from front.hutaba.client.img2chan import _normalize_img_post_no  # type: ignore[attr-defined]
except Exception:
    def _normalize_img_post_no(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.search(r"No\.(\d+)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"(?<!\d)(\d{6,})(?!\d)", text)
        return match.group(1) if match else ""

try:
    from harness.agent.prompt_parser.prompt_manager.budget import sanitize_memory_candidates
except Exception:
    def sanitize_memory_candidates(memory_lines: list[str], last_assistant_text: str = "") -> list[str]:
        return [str(line).strip() for line in memory_lines if str(line).strip()]

try:
    from harness.agent.tool_registry.web_research.helpers import extract_named_term_candidates
except Exception:
    extract_named_term_candidates = None  # type: ignore[assignment]

try:
    from harness.agent.memory_manager.logic import (
        SlangEntry,
        filter_query_terms_by_slang,
        format_slang_guidance_for_prompt,
        lookup_slang_in_text,
    )
except (ImportError, AttributeError):
    # 他モジュール未完了時・単体テスト用のセーフフォールバック
    _LOCAL_PREFIX_SLANG = {"すみフラ", "うろフラ", "フラ"}
    SlangEntry = dict[str, Any]  # type: ignore[misc,assignment]

    def lookup_slang_in_text(text: str) -> list[dict[str, Any]]:  # type: ignore[misc]
        matched = []
        for term in _LOCAL_PREFIX_SLANG:
            if term in text:
                matched.append({
                    "term": term,
                    "meaning": "質問・相談の前置き略語",
                    "guidance": "キャラクター名や作品名ではなく、質問・相談の前置き略語です。",
                    "strip_from_query": True,
                })
        return matched

    def filter_query_terms_by_slang(terms: list[str]) -> list[str]:
        return [t for t in terms if t.strip() not in _LOCAL_PREFIX_SLANG and not any(p in t for p in ("すみフラ", "うろフラ"))]

    def format_slang_guidance_for_prompt(entries: list[dict[str, Any]]) -> list[str]:  # type: ignore[misc]
        return [f"- 『{e['term']}』: {e.get('guidance', e.get('meaning', ''))}" for e in entries if 'term' in e]


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
    return deduped


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


# ペルソナの痕跡とみなす語尾・マーカー（部分文字列）
_PERSONA_TONE_MARKERS: tuple[str, ...] = (
    "んですよ", "ですよ", "ますよ", "ですね", "ますね", "んです",
    "ちゃい", "ちゃう", "ぴゃっ",
)
# 匿名掲示板の常体とみなす文末（文の終わりに現れた場合のみ）
_PLAIN_TONE_ENDING_RE = re.compile(
    r"(だからな|だろな|だよな|だなぁ|だなあ|だな|じゃね|だぜ)(?=[…‥。]*$)",
    re.MULTILINE,
)


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


def find_persona_tone_loss(
    body: str,
    *,
    first_person: str = "",
    exempt_max_chars: int = 20,
    markers: Iterable[str] | None = None,
) -> str | None:
    """ペルソナ口調の喪失を検出する。違反があれば理由文字列、なければ None。

    次の2条件を両方満たす場合のみ違反とする（保守的に倒す）:
    A: ペルソナのマーカー（語尾・一人称）が本文に1つも無い
    B: 掲示板常体の文末が1つ以上ある
    免除: 本文が exempt_max_chars 字以下（定型句・短いツッコミ・大喜利の一言はペルソナを外すことが正当なため）。
    """
    raw = _extract_hutaba_non_quote_body(body).strip()
    if not raw:
        return None

    # 免除: 短い定型句・ツッコミ・大喜利
    if len(raw) <= exempt_max_chars:
        return None

    # 実効マーカーの決定: None または空の場合はモジュール既定定数へフォールバック
    effective_markers: tuple[str, ...]
    if markers is None:
        effective_markers = _PERSONA_TONE_MARKERS
    else:
        normalized_markers = tuple(m.strip() for m in markers if m and m.strip())
        effective_markers = normalized_markers if normalized_markers else _PERSONA_TONE_MARKERS

    # 条件A: ペルソナのマーカー（語尾または一人称）が本文に1つでもあるか
    has_persona_marker = any(m in raw for m in effective_markers)
    if not has_persona_marker and first_person and first_person.strip():
        has_persona_marker = first_person.strip() in raw

    if has_persona_marker:
        return None

    # 条件B: 掲示板常体の文末が1つ以上あるか
    m = _PLAIN_TONE_ENDING_RE.search(raw)
    if m:
        ending = m.group(1)
        return f"ペルソナ語尾・一人称が皆無で、常体文末（{ending}）が含まれています"

    return None


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


_SELF_PROJECTION_SYMPATHY_RE = re.compile(
    r"(?:私|わたし|あたし|僕|ぼく|俺|おれ|自分|うち)も[^\n。！？!?]{0,35}?"
    r"(?:困ったこと(?:が)?ある|探してて困った|悩んだこと(?:が)?ある|途方に暮れた|"
    r"分からなくて困った|わからなくて困った|経験(?:が)?あるんですよ|同じこと思った)",
    re.IGNORECASE,
)
# ※ "RSS" 単体は質問文トピック名詞であり「RSSはやめてる所多いですよね」等の相槌行に現れるため、解決策アクション語から除外
_SOLUTION_HINT_MARKER_RE = re.compile(
    r"(?:クロール|スクレイピング|チェック|確認|探すと|探ったら|残って|放置|ツール|スクリプト|拡張機能|通知|差分|巡回|代替|使うと|すれば|しか無い|しかない)",
    re.IGNORECASE,
)
_HUTABA_META_SEARCH_REPORT_RE = re.compile(
    r"(?:調べてみた(?:けど|んですが|んですけど|ところ)|検索してみた(?:けど|んですが|んですけど)|"
    r"ググってみた(?:けど|んですが|んですけど)|"
    r"定義までは確認でき|意味までは確認でき|詳細までは確認でき|"
    r"ニュースばかりが並|記事ばかりが並|パーティーとか[^\n。！？!?]{0,20}ばかり|"
    r"特定の作品のネタか|別の固有名詞のことですか|何のことか確認でき)",
    re.IGNORECASE,
)
_HUTABA_META_COMMENTARY_RE = re.compile(
    r"(?:(?:ツッコミ|指摘|声|レス|意見|判定)(?:が|も)?(?:来て|入って|あって|言われて|飛んで)?(?:ます|る|ますけど|るけど|るようですが|そうですけど)|"
    r"と言われて(?:ます|る|います)|とツッコミ(?:が|入って)|って言われてる)",
    re.IGNORECASE,
)


from typing import TYPE_CHECKING


_HUTABA_HONORIFIC_NAME_PATTERN = re.compile(
    r"(?:(?<=[^\w])|(?<=^))([A-Za-z0-9_\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]{1,16})(?:さん|氏|くん|ちゃん|様)(?=[、。！？\s　]|$)",
    flags=re.UNICODE,
)


def _clean_hutaba_thought_tags(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


_STREAM_URL_PATTERN = re.compile(
    r"(?:"
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?|live/|shorts/)|youtu\.be/)|"
    r"https?://(?:www\.)?twitch\.tv/|"
    r"https?://(?:live\d*\.)?nicovideo\.jp/|"
    r"https?://(?:www\.)?twitcasting\.tv/|"
    r"https?://(?:www\.)?showroom-live\.com/|"
    r"https?://(?:www\.)?openrec\.tv/|"
    r"(?:bin/)?jump\.php\?.*?(?:youtube\.com|youtu\.be|twitch\.tv|nicovideo\.jp)"
    r")",
    flags=re.IGNORECASE,
)

_STREAM_TITLE_KEYWORD_PATTERN = re.compile(
    r"(?:"
    r"【[^】]*(?:配信|実況|歌枠|生放送|耐久|同時視聴|お披露目|記念|リレー|LIVE|Live)[^】]*】|"
    r"(?:生?配信|生放送|実況|歌枠|耐久配信|飲酒配信|同時視聴|作業配信|朝活|メン限|"
    r"歌枠リレー|建てるぜ|次スレ|避難所|本スレ)"
    r")",
    flags=re.IGNORECASE,
)


def _is_img2chan_stream_or_live_thread(thread_item: dict[str, Any]) -> str | None:
    """配信・実況スレであるかを判定し、該当する場合は理由文字列を返す（健全な雑談スレなら None）。"""
    if not cfg_bool("HUTABA_EXCLUDE_STREAM_THREADS", True):
        return None

    text = str((thread_item or {}).get("text") or "")
    title = str((thread_item or {}).get("title") or "")
    full_content = f"{title}\n{text}".strip()

    # 1. 配信・動画URLの検知
    if _STREAM_URL_PATTERN.search(full_content):
        return "stream_url_detected"

    # 2. 配信・実況特有キーワードの検知
    if _STREAM_TITLE_KEYWORD_PATTERN.search(title) or _STREAM_TITLE_KEYWORD_PATTERN.search(text[:80]):
        return "stream_keyword_detected"

    return None


_REQUEST_THREAD_PATTERN = re.compile(
    r"(?:"
    # 1. 括弧内のリクエスト・お題表記
    r"【[^】]*(?:リクエスト|リク|お題募集|ネタ募集)[^】]*】|"
    # 2. 接頭辞（AI, 画像生成, イラスト, お絵描き, 手描き, 絵描き, 絵, コラ） + リクエスト/リク
    r"(?:AI|画像生成|イラスト|お絵描き|手描き|絵描き|絵|コラ)\s*(?:リクエスト|リク)(?:スレ|受付|募集|会場|所|部屋)?|"
    # 3. リクエスト/リク + 接尾辞（スレ, 受付, 募集, 会場, 所, 部屋）
    r"(?:リクエスト|リク)\s*(?:スレ|受付|募集|会場|所|部屋)|"
    # 4. リクエスト/リク + 呼びかけ・促し（どうぞ, ある, あれば, あります, ください, 待ってます, お願いします, はこちら）
    r"(?:リクエスト|リク)\s*(?:どうぞ|ある|あれば|あります|ください|待ってます|お願いします|はこちら)|"
    # 5. お題・ネタの募集
    r"(?:お題|ネタ)\s*(?:募集|リクエスト)(?:スレ)?"
    r")",
    flags=re.IGNORECASE,
)


def _is_img2chan_request_thread(thread_item: dict[str, Any]) -> str | None:
    """リクエスト・お題募集スレであるかを判定し、該当する場合は理由文字列を返す（健全な雑談スレなら None）。"""
    if not cfg_bool("HUTABA_EXCLUDE_REQUEST_THREADS", True):
        return None

    title = str((thread_item or {}).get("title") or "")
    text = str((thread_item or {}).get("text") or (thread_item or {}).get("op_text") or "")

    # 1. タイトルチェック
    if _REQUEST_THREAD_PATTERN.search(title):
        return "request_title_detected"

    # 2. 本文冒頭（100文字）チェック
    if _REQUEST_THREAD_PATTERN.search(text[:100]):
        return "request_keyword_detected"

    return None


def _is_hutaba_thread_expiring_soon(
    thread_or_item: dict[str, Any] | None,
    *,
    min_remaining_sec: float | None = None,
) -> str | None:
    """スレッドが消滅間近（デフォルト10分=600秒以内、または古いスレ警告あり）か判定する。
    除外すべき場合は理由文字列を返し、問題なければ None を返す。
    """
    if not isinstance(thread_or_item, dict):
        return None
    if not cfg_bool("HUTABA_EXCLUDE_EXPIRING_THREADS", True):
        return None

    threshold_sec = (
        float(min_remaining_sec)
        if min_remaining_sec is not None
        else max(60.0, cfg_float("HUTABA_MIN_THREAD_REMAINING_SEC", 600.0))
    )

    if bool(thread_or_item.get("has_old_warning")):
        return "old_thread_warning"

    rem_sec = thread_or_item.get("remaining_sec")
    if isinstance(rem_sec, (int, float)) and not isinstance(rem_sec, bool):
        if float(rem_sec) <= threshold_sec:
            return f"remaining_sec_too_short:{int(float(rem_sec))}s<={int(threshold_sec)}s"
        return None

    if bool(thread_or_item.get("is_expiring_soon")):
        return "expiring_soon_flag"

    # 生テキスト（expire_text 等）のみが存在する場合のフォールバック解析
    raw_expire = str(thread_or_item.get("expire_text") or "").strip()
    if raw_expire:
        helper = _get_img2chan_helper()
        parse_exp = getattr(helper, "parse_hutaba_thread_expiration", None)
        if callable(parse_exp):
            exp_info = parse_exp(raw_expire, min_remaining_sec=threshold_sec)
            if isinstance(exp_info, dict) and bool(exp_info.get("is_expiring_soon")):
                calc_rem = exp_info.get("remaining_sec")
                if isinstance(calc_rem, (int, float)):
                    return f"remaining_sec_too_short:{int(float(calc_rem))}s<={int(threshold_sec)}s"
                return "expiring_soon_parsed"

    return None


def _extract_hutaba_thread_topic_snippet(thread: dict[str, Any] | None, max_len: int = 18) -> str:
    """スレッドタイトルまたはOP本文から簡潔な話題スニペットを抽出。"""
    t = thread if isinstance(thread, dict) else {}
    title = str(t.get("title") or "").strip()
    if not title:
        title = str(t.get("op_text") or "").strip()
    # URLや余計な空白・改行・記号を除去
    title = re.sub(r"https?://\S+", "", title)
    title = re.sub(r"[\r\n\t]+", " ", title).strip()
    title = title.strip(" 　!！?？#＃@＠")
    if not title:
        return "雑談"
    if len(title) > max_len:
        return title[:max_len] + "…"
    return title


def _extract_hutaba_comment_snippet(comment: str, max_len: int = 14) -> str:
    """投稿コメントから引用行（>）や改行を除去した簡潔な要約を抽出。"""
    cleaned = re.sub(r"^>.*$", "", comment, flags=re.MULTILINE)
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).strip()
    if not cleaned:
        cleaned = comment.strip()
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def _build_hutaba_post_emotion_reason(
    thread: dict[str, Any] | None,
    comment: str,
    target_reply: dict[str, Any] | None = None,
    dominant_emotion: str = "joy",
    max_chars: int = 55,
    *,
    url: str = "",
    board: str = "",
) -> str:
    """ふたばスレッド書き込み後の感情理由テキスト（LLM未使用・失敗時のフォールバック）。"""
    t = thread if isinstance(thread, dict) else {}
    raw_url = str(url or t.get("url") or "").lower()
    raw_board = str(board or t.get("board") or "").lower()
    srv = "may" if ("may.2chan.net" in raw_url or raw_board == "may") else "img"
    board_label = f"ふたば({srv})"

    topic = _extract_hutaba_thread_topic_snippet(t, max_len=20)
    comment_snip = _extract_hutaba_comment_snippet(comment, max_len=16)
    has_target = isinstance(target_reply, dict) and bool(str(target_reply.get("text") or "").strip())

    topic_part = f"「{topic}」" if topic else ""
    dom = str(dominant_emotion or "joy").strip().lower()

    if dom in ("joy", "fun", "happy"):
        if has_target:
            reason = f"{board_label}の{topic_part}スレでとしあきと掛け合いして楽しかった。"
        else:
            reason = f"{board_label}の{topic_part}スレに書き込んで楽しかった。"
    elif dom == "surprise":
        if has_target:
            reason = f"{board_label}の{topic_part}スレでとしあきの予想外な反応に驚いた。"
        else:
            reason = f"{board_label}の{topic_part}スレで意外な展開を見て驚いた。"
    elif dom in ("anticipation", "interest"):
        reason = f"{board_label}の{topic_part}スレの話題が気になって書き込んだ。"
    elif dom == "trust":
        reason = f"{board_label}の{topic_part}スレでとしあき達と共感し合った。"
    elif dom == "sadness":
        reason = f"{board_label}の{topic_part}スレで切ない話題にしんみりした。"
    elif dom in ("anger", "disgust", "fear"):
        reason = f"{board_label}の{topic_part}スレで少し戸惑いつつレスした。"
    else:  # neutral / その他
        if comment_snip:
            reason = f"{board_label}の{topic_part}スレで「{comment_snip}」とレスした。"
        else:
            reason = f"{board_label}の{topic_part}スレでとしあきと雑談した。"

    if max_chars and len(reason) > max_chars:
        reason = reason[: max_chars - 1] + "…"
    return reason


_SIBLING_PERSONA_SUFFIX_RE = re.compile(
    r"(?:なんですよ|ですよ|ですよね|ですね|ますよね|ますよ|ましたよ|ましたね|じゃないですか|"
    r"だけど|けれど|けど|さすがに|な話|だろな|だろうな|だろ|よな|よね|だな|かな|わね|わよ|"
    r"私は|私も|俺は|俺も|自分は|自分も|[…!！?？。、,.・\s])+"
)
_SIBLING_SYNONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("落ちて", "下がって"),
    ("落ちた", "下がった"),
    ("落ちる", "下がる"),
    ("暴落して", "下がって"),
)


def _extract_hutaba_non_quote_body(text: Any) -> str:
    lines = [
        line.strip()
        for line in str(text or "").splitlines()
        if line.strip() and not line.strip().startswith(">")
    ]
    return "\n".join(lines).strip()


def _strip_hutaba_quote_lines(text: Any) -> str:
    return _extract_hutaba_non_quote_body(text)


def _normalize_for_sibling_plagiarism_check(text: Any) -> str:
    """他レス盗用・つぎはぎ判定用に、一人称・語尾・記号・定型フィラーを除去して正規化する。"""
    s = _strip_hutaba_quote_lines(text)
    s = re.sub(r">>?\s*\d+", "", s)
    s = unicodedata.normalize("NFKC", s).lower()
    for src, dst in _SIBLING_SYNONYM_PAIRS:
        s = s.replace(src, dst)
    # 句読点・記号・空白の除去前に、文頭の一人称や文末の定型語尾を境界付きで除去する
    s = re.sub(
        r"(?:^|(?<=[\s、。！？!?…・]))(?:わたし|私|あたし|僕|ぼく|俺|おれ|自分|うち)(?:は|も|が|の|的に|と)?",
        "",
        s,
    )
    s = re.sub(
        r"(?:です(?:ね|よ|よね|か)?|ます(?:ね|よ|よね|か)?|だ(?:ね|よ|な|よね|なぁ|なあ)|"
        r"ですよ(?:ね)?|ますよ(?:ね)?|かも(?:しれません|しれない|ですね|です)?|"
        r"かな(?:ぁ|あ)?|だよな(?:ぁ|あ)?|ですねぇ|ますねぇ|ちゃう(?:よね|ね|な)?|"
        r"ちゃいます(?:よね|ね|よ)?|ん(?:だよ|だな|ですね|ですよ|だよね))(?=[\s、。！？!?…・]*$)",
        "",
        s,
    )
    s = re.sub(r"[\s\u3000、。，．・…‥!！?？「」『』（）()［］\[\]〜~ー―\-:;\"'`]+", "", s)
    return s


def _sanitize_hutaba_reply_text(
    text: str,
    *,
    max_lines: int = 4,
    max_chars: int = 180,
) -> str:
    """ふたば（img）向けレス文のサニタイズ。
    思考タグ除去、相手呼称（特定個人名）排除、Shift_JIS互換性維持、文字数制限。
    """
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return ""

    # JSONのcommentフィールド内にLLMが誤って「>対象レス本文」の引用行を含めた場合、
    # extract_first_user_facing_reply が先頭の「>」だけを剥がして本文に混入させるのを防ぐため、
    # 他に通常の本文行がある場合は非アンカーの引用行（>数字 以外）を事前に除去する。
    raw_lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    anchor_lines: list[str] = []
    body_lines: list[str] = []
    for ln in raw_lines:
        if re.match(r"^[>＞]+\s*\d+", ln):
            anchor_lines.append(ln)
        elif not re.match(r"^[>＞]", ln):
            body_lines.append(ln)

    if body_lines:
        raw_to_clean = "\n".join(body_lines)
    else:
        raw_to_clean = "\n".join(raw_lines)

    raw_to_clean = _clean_hutaba_thought_tags(raw_to_clean)
    cleaned_body = extract_first_user_facing_reply(raw_to_clean) or sanitize_generated_reply(raw_to_clean)
    cleaned_body = cleaned_body.replace("\r\n", "\n").replace("\r", "\n")

    # アンカー引用行があれば先頭に復元
    if anchor_lines and cleaned_body:
        raw = "\n".join([*anchor_lines, cleaned_body])
    elif anchor_lines:
        raw = "\n".join(anchor_lines)
    else:
        raw = cleaned_body

    # 相手呼称の抑止（「○○さん、」「○○さん」などの捏造個人名を削除）
    raw = _HUTABA_HONORIFIC_NAME_PATTERN.sub("", raw)

    # Bot名や自己紹介的な名乗りの除去
    display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
    if display_name:
        raw = re.sub(rf"(?:私|わたし|僕|ボク)は{re.escape(display_name)}[ですだ]", "", raw)

    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    # 文末等の「…！」が1レス内で複数回連発されるのを抑止（最初の1回のみ維持し、2回目以降は「。」へ正規化）
    ellipsis_bang_pattern = re.compile(r"(?:…+|\.{2,})[！!]+")
    if len(ellipsis_bang_pattern.findall(raw)) >= 2:
        seen_first = False

        def _replace_extra_ellipsis_bang(match: re.Match[str]) -> str:
            nonlocal seen_first
            if not seen_first:
                seen_first = True
                return match.group(0)
            return "。"

        raw = ellipsis_bang_pattern.sub(_replace_extra_ellipsis_bang, raw)
        raw = re.sub(r"。{2,}", "。", raw)

    try:
        raw.encode("cp932")
    except UnicodeEncodeError:
        raw = raw.encode("cp932", errors="ignore").decode("cp932")

    helper = _get_img2chan_helper()
    if helper is not None and hasattr(helper, "sanitize_img_post_text"):
        raw = getattr(helper, "sanitize_img_post_text")(raw, max_chars=max_chars)
    else:
        raw = truncate_text(raw, max_chars).strip()

    return raw


_HUTABA_ANGER_OR_DENIAL_PATTERNS = [
    r"(?:ちがう|違う|そうじゃない|言ってない|言っとらん)",
    r"(?:いい加減にしろ|ふざけんな|黙れ|うぜえ|ウザい|消えろ)",
    r"(?:なんなん|何言ってんだ|会話にならん|会話になってない)",
    r"[！!]{3,}",
]


def _looks_like_hutaba_anger_or_denial(text: str) -> bool:
    raw = str(text or "")
    return any(re.search(p, raw) for p in _HUTABA_ANGER_OR_DENIAL_PATTERNS)


if TYPE_CHECKING:
    from ollama_bot.ollama_chat_.ollama_chat_types import OllamaChatProtocol
    _Img2chanPostingBase = OllamaChatProtocol
else:
    _Img2chanPostingBase = object


class ParrotReason(str):
    """オウム返し・盗用検知理由を表す文字列型。観測用の詳細メタデータを保持する。"""
    matched_sibling_post_nos: list[int]
    longest_common_subsequence_len: int

    def __new__(
        cls,
        reason: str,
        *,
        matched_sibling_post_nos: list[int] | None = None,
        longest_common_subsequence_len: int = 0,
    ) -> ParrotReason:
        obj = super().__new__(cls, reason)
        obj.matched_sibling_post_nos = list(matched_sibling_post_nos or [])
        obj.longest_common_subsequence_len = int(longest_common_subsequence_len)
        return obj


class _Img2chanPostingMixin(_Img2chanPostingBase):
    def _img2chan_proactive_post_enabled(self) -> bool:
        return cfg_bool("HUTABA_PROACTIVE_POST_ENABLED", True)

    def _img2chan_proactive_post_interval_min_sec(self) -> float:
        return max(cfg_float("HUTABA_PROACTIVE_POST_INTERVAL_MIN_SEC", 1800.0), 10.0)

    def _img2chan_proactive_post_interval_max_sec(self) -> float:
        min_sec = self._img2chan_proactive_post_interval_min_sec()
        return max(cfg_float("HUTABA_PROACTIVE_POST_INTERVAL_MAX_SEC", 3600.0), min_sec)

    def _img2chan_autonomous_followup_cooldown_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC", 300.0), 30.0)

    def _is_discord_user_reply_busy(self) -> bool:
        """Discord側でユーザー返信が推論中・前処理中・またはキュー待機中であるかを判定する。"""
        busy_fn = getattr(self, "_is_user_reply_busy", None)
        if callable(busy_fn):
            try:
                if bool(busy_fn()):
                    return True
            except Exception:
                pass
        bot = getattr(self, "bot", None)
        if bot is not None:
            busy_fn_bot = getattr(bot, "_is_user_reply_busy", None)
            if callable(busy_fn_bot):
                try:
                    if bool(busy_fn_bot()):
                        return True
                except Exception:
                    pass
        if getattr(self, "_active_user_reply_count", 0) > 0:
            return True
        if bot is not None and getattr(bot, "_active_user_reply_count", 0) > 0:
            return True
        if getattr(self, "_active_user_reply_turn_count", 0) > 0:
            return True
        if bot is not None and getattr(bot, "_active_user_reply_turn_count", 0) > 0:
            return True
        q = getattr(self, "_reply_queue", None)
        if q is None and bot is not None:
            q = getattr(bot, "_reply_queue", None)
        if q is not None and hasattr(q, "qsize"):
            try:
                if int(q.qsize()) > 0:
                    return True
            except Exception:
                pass
        return False

    async def _wait_for_discord_user_replies_idle(self, timeout_sec: float = 30.0) -> None:
        """Discordのユーザー返信および返信キューが完全にアイドルになるまで待機・譲歩する。"""
        parent_yielder = getattr(super(), "_yield_to_user_replies", None)
        if callable(parent_yielder):
            await parent_yielder(timeout_sec=timeout_sec)
        bot = getattr(self, "bot", None)
        bot_yielder = getattr(bot, "_yield_to_user_replies", None) if bot is not None else None
        if callable(bot_yielder):
            await bot_yielder(timeout_sec=timeout_sec)

        start_ts = time.time()
        max_wait = max(timeout_sec, float(cfg("LLM_YIELD_MAX_WAIT_SEC", 300.0) or 300.0))
        if self._is_discord_user_reply_busy():
            log.info("[hutaba] Discord返信中・キュー待機中を検知、処理を譲歩・待機開始")
            while self._is_discord_user_reply_busy():
                if time.time() - start_ts >= max_wait:
                    log.warning("[hutaba] Discord返信アイドル待機がタイムアウトしました (%.1fs)", max_wait)
                    break
                done_event = getattr(self, "_user_reply_done_event", None)
                if done_event is None and bot is not None:
                    done_event = getattr(bot, "_user_reply_done_event", None)
                if done_event is not None and hasattr(done_event, "wait"):
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(done_event.wait(), timeout=0.3)
                else:
                    await asyncio.sleep(0.3)
            log.info("[hutaba] Discord返信完了または待機終了を検知")

    async def _yield_to_user_replies(self, timeout_sec: float = 30.0) -> None:
        """Discordユーザー返信最優先（Preemption）ガード。
        ユーザー返信生成中・推論前処理中・返信キュー待機中は即座に待機・譲歩する。
        """
        await self._wait_for_discord_user_replies_idle(timeout_sec=timeout_sec)

    def _img2chan_exclude_stream_threads(self) -> bool:
        getter = getattr(super(), "_img2chan_exclude_stream_threads", None)
        if callable(getter):
            return bool(getter())
        return cfg_bool("HUTABA_EXCLUDE_STREAM_THREADS", True)

    def _is_img2chan_stream_or_live_thread(self, thread_item: dict[str, Any]) -> str | None:
        checker = getattr(super(), "_is_img2chan_stream_or_live_thread", None)
        if callable(checker):
            return checker(thread_item)
        return _is_img2chan_stream_or_live_thread(thread_item)

    def _img2chan_exclude_request_threads(self) -> bool:
        getter = getattr(super(), "_img2chan_exclude_request_threads", None)
        if callable(getter):
            return bool(getter())
        return cfg_bool("HUTABA_EXCLUDE_REQUEST_THREADS", True)

    def _is_img2chan_request_thread(self, thread_item: dict[str, Any]) -> str | None:
        checker = getattr(super(), "_is_img2chan_request_thread", None)
        if callable(checker):
            return checker(thread_item)
        return _is_img2chan_request_thread(thread_item)

    def _is_hutaba_thread_expiring_soon(
        self,
        thread_or_item: dict[str, Any] | None,
        *,
        min_remaining_sec: float | None = None,
    ) -> str | None:
        return _is_hutaba_thread_expiring_soon(
            thread_or_item,
            min_remaining_sec=min_remaining_sec,
        )

    def _is_img2chan_thread_vandalized(self, url: str) -> bool:
        checker = getattr(super(), "_is_img2chan_thread_vandalized", None)
        if callable(checker):
            return bool(checker(url))
        vandalized = getattr(self, "_img2chan_vandalized_urls", None)
        return bool(isinstance(vandalized, set) and url in vandalized)

    def _mark_img2chan_thread_vandalized(self, url: str, reason: str = "") -> None:
        marker = getattr(super(), "_mark_img2chan_thread_vandalized", None)
        if callable(marker):
            marker(url, reason)
            return
        vandalized = getattr(self, "_img2chan_vandalized_urls", None)
        if isinstance(vandalized, set):
            vandalized.add(url)
        recent = getattr(self, "_img2chan_recent_schedule", None)
        if isinstance(recent, dict):
            recent[url] = time.monotonic()

    def _save_img2chan_checkpoint(self, thread_state: dict[str, Any], **kwargs: Any) -> None:
        saver = getattr(super(), "_save_img2chan_checkpoint", None)
        if callable(saver):
            saver(thread_state, **kwargs)
            return
        thread_state["checkpoint"] = {"saved_at": time.time(), **kwargs}

    def _restore_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> dict[str, Any] | None:
        restorer = getattr(super(), "_restore_img2chan_checkpoint", None)
        if callable(restorer):
            res = restorer(thread_state)
            return res if isinstance(res, dict) else None
        checkpoint = thread_state.get("checkpoint")
        return checkpoint if isinstance(checkpoint, dict) else None

    def _clear_img2chan_checkpoint(self, thread_state: dict[str, Any]) -> None:
        cleaner = getattr(super(), "_clear_img2chan_checkpoint", None)
        if callable(cleaner):
            cleaner(thread_state)
            return
        thread_state["checkpoint"] = None

    def _detect_img2chan_thread_vandalism(
        self,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
    ) -> str | None:
        """荒らし・不毛スレを検知し、撤退理由文字列を返す（健全な場合は None）。"""
        from front.hutaba.monitor.img2chan_learning import _IMG2CHAN_VANDALISM_BLOCK_PATTERNS

        op_text = str((thread or {}).get("op_text") or "").strip()
        if op_text and _IMG2CHAN_VANDALISM_BLOCK_PATTERNS.search(op_text):
            return "op_blocked_pattern"

        replies = list((thread or {}).get("replies") or [])
        if not replies:
            return None

        recent_replies = replies[-20:]
        block_hit_count = 0
        reply_texts: list[str] = []

        for r in recent_replies:
            t = str(r.get("text") or "").strip()
            if not t:
                continue
            reply_texts.append(t)
            if _IMG2CHAN_VANDALISM_BLOCK_PATTERNS.search(t):
                block_hit_count += 1

        if reply_texts and (block_hit_count / len(reply_texts)) >= 0.30:
            return f"high_toxicity_ratio ({block_hit_count}/{len(reply_texts)})"

        if len(reply_texts) >= 5:
            from collections import Counter
            counts = Counter(reply_texts)
            most_common_text, most_common_count = counts.most_common(1)[0]
            if most_common_count >= 4 or (len(reply_texts) >= 6 and most_common_count / len(reply_texts) >= 0.5):
                return f"script_spam_detected (repeated {most_common_count} times)"

        symbol_heavy_count = 0
        for t in reply_texts:
            if len(t) >= 10:
                alnum_jp = len(re.findall(r"[\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", t))
                if alnum_jp / len(t) < 0.2:
                    symbol_heavy_count += 1
        if reply_texts and (symbol_heavy_count / len(reply_texts)) >= 0.40:
            return f"symbol_spam_bomb ({symbol_heavy_count}/{len(reply_texts)})"

        return None

    def _sanitize_hutaba_reply_text(self, text: str, max_chars: int = 120) -> str:
        """ふたば（img）向けレス文のサニタイズ。
        思考タグ除去、相手呼称（特定個人名）排除、Shift_JIS互換性維持、文字数制限。
        """
        return _sanitize_hutaba_reply_text(text, max_chars=max_chars)

    def _img2chan_character_guidance(self) -> str:
        parts: list[str] = []
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            parts.append(f"人格名: {display_name}")
        first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
        if first_person:
            parts.append(f"一人称: {first_person}")
        core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()
        if core_values:
            parts.append(f"核となる価値観・性格: {core_values}")
        for label, key in (
            ("口調ガード", "OLLAMA_REPLY_STYLE_GUARD"),
            ("img投稿追加ガード", "IMG2CHAN_POST_PERSONA_GUARD"),
        ):
            value = str(cfg(key, "") or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        if not parts:
            return ""
        guidance = "\n".join(parts)
        max_chars = max(cfg_int("IMG2CHAN_CHARACTER_GUIDANCE_MAX_CHARS", 1600), 200)
        return truncate_text(guidance, max_chars)

    def _img2chan_post_delay_sec(self) -> float:
        min_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MIN_DELAY_SEC", 60.0), 0.0)
        max_delay = max(cfg_float("IMG2CHAN_AUTO_POST_MAX_DELAY_SEC", 1800.0), min_delay)
        return random.uniform(min_delay, max_delay)

    def _img2chan_post_generation_retries(self) -> int:
        return max(cfg_int("IMG2CHAN_POST_GENERATION_RETRIES", 1), 0)

    def _img2chan_post_generation_models(self) -> list[str]:
        models: list[str] = []
        for raw in (
            cfg("IMG2CHAN_POST_GENERATION_MODEL", cfg("OLLAMA_MODEL", "")),
            cfg("IMG2CHAN_POST_FALLBACK_MODEL", cfg("OLLAMA_MIDDLE_MODEL", "")),
        ):
            model = str(raw or "").strip()
            if model and model not in models:
                models.append(model)
        return models

    def _img2chan_promotion_enabled(self) -> bool:
        return cfg_bool("IMG2CHAN_PROMOTION_ENABLED", False)

    def _img2chan_promotion_probability(self) -> float:
        return max(0.0, min(cfg_float("IMG2CHAN_PROMOTION_PROBABILITY", 0.0), 1.0))

    def _img2chan_promotion_min_interval_sec(self) -> float:
        return max(cfg_float("IMG2CHAN_PROMOTION_MIN_INTERVAL_SEC", 0.0), 0.0)

    def _img2chan_promotion_invite_url(self) -> str:
        invite_url = str(cfg("IMG2CHAN_PROMOTION_INVITE_URL", "") or "").strip()
        if invite_url and re.match(r"^discord\.gg/[A-Za-z0-9_-]+$", invite_url, flags=re.IGNORECASE):
            return f"https://{invite_url}"
        return invite_url

    def _img2chan_promotion_message(self) -> str:
        message = str(cfg("IMG2CHAN_PROMOTION_MESSAGE", "") or "").strip()
        if message:
            return message
        display_name = str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or "").strip()
        if display_name:
            return f"普段はDiscordの{display_name}でも喋ってる。"
        return "普段はDiscordでも喋ってる。"

    def _should_attach_img2chan_promotion(self) -> bool:
        if not self._img2chan_promotion_enabled():
            return False
        if not self._img2chan_promotion_invite_url():
            return False
        probability = self._img2chan_promotion_probability()
        if probability <= 0.0:
            return False
        last_sent_at = float(getattr(self, "_img2chan_last_promotion_at", 0.0) or 0.0)
        min_interval_sec = self._img2chan_promotion_min_interval_sec()
        if last_sent_at > 0.0 and min_interval_sec > 0.0 and time.time() - last_sent_at < min_interval_sec:
            return False
        return random.random() < probability

    def _log_img2chan_thread_analysis(
        self,
        *,
        context: str,
        url: str,
        thread: dict[str, Any],
        message: discord.Message | None = None,
    ) -> None:
        helper = _get_img2chan_helper()
        formatter = getattr(helper, "format_img_thread_log_summary", None) if helper is not None else None
        try:
            if callable(formatter):
                summary = formatter(
                    thread,
                    max_replies=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_REPLIES", 5), 0),
                    max_chars=max(cfg_int("IMG2CHAN_THREAD_LOG_MAX_CHARS", 1400), 300),
                )
            else:
                summary = (
                    f"IMG thread parse url={url} "
                    f"thread_no={str((thread or {}).get('thread_no') or '') or '-'} "
                    f"replies={int((thread or {}).get('total_reply_count') or 0)} "
                    f"fetched={int((thread or {}).get('fetched_reply_count') or 0)} "
                    f"op={truncate_text(str((thread or {}).get('op_text') or '').strip(), 160)!r}"
                )
        except Exception as e:
            log.warning("[hutaba] スレッド分析ログ整形失敗 url=%s err=%r", url, e)
            return

        log.info(
            "[hutaba] スレッド分析完了 context=%s channel=%s author=%s\n%s",
            context,
            channel_id(message) if message is not None else None,
            author_id(message) if message is not None else None,
            summary,
        )

    def _mark_img2chan_promotion_sent(self) -> None:
        self._img2chan_last_promotion_at = time.time()

    def _maybe_append_img2chan_promotion(
        self,
        helper: Any,
        comment: str,
    ) -> tuple[str, bool, str]:
        current_comment = str(comment or "").strip()
        invite_url = self._img2chan_promotion_invite_url()
        if not current_comment or not invite_url or not self._should_attach_img2chan_promotion():
            return current_comment, False, ""

        promo_message = self._img2chan_promotion_message()
        builder = getattr(helper, "append_img_post_promotion", None) if helper is not None else None
        if callable(builder):
            promoted_comment = builder(
                current_comment,
                promo_message,
                invite_url,
                max_chars=340,
            )
        else:
            promoted_comment = "\n".join(
                part for part in (current_comment, promo_message, invite_url) if str(part or "").strip()
            )
        promoted_comment = str(promoted_comment or "").strip()
        if not promoted_comment or invite_url not in promoted_comment:
            return current_comment, False, ""
        return promoted_comment, True, invite_url

    def _build_img2chan_followup_retry_prompt(
        self,
        base_prompt: str,
        *,
        target_reply: dict[str, Any] | None = None,
    ) -> str:
        excerpt = truncate_text(
            re.sub(r"\s+", " ", str((target_reply or {}).get("text") or "").strip()),
            120,
        )
        lines = [
            str(base_prompt or "").strip(),
            "",
            "追加ルール:",
            "- 直前の候補はオウム返し寄り、または不適切な検索報告・自己投影だったため破棄してください。",
            "- 今回返す対象レスの本文や語尾をそのままなぞるのは禁止です。",
            "- 対象レスの言い換えだけで終わらせず、自分の反応やツッコミを自分の言葉で入れてください。",
            "- 出力は引き続きJSONだけにし、comment には新しく書く本文だけを入れてください。",
            "- 対象レスの一部の行動や単語（例：『手順書を見直す』等）だけを拾って『私も毎回〜派なんですよ』『自分も〜するタイプです』のように主語を自分にすり替えて同調するだけの自己投影ミラーリングも禁止です。",
            "- 『調べてみたんですけど定義が確認できなかった』『〜のニュースばかり出てきた』『特定の作品のネタ？』といった検索失敗レポートや辞書解説は【絶対厳禁】です。事前調査メモにスレと無関係なノイズがある場合は完全に無視し、前後のレスの流れに対する等身大のツッコミ・共感レスを書いてください。",
            "- 自分の習慣語りにすり替えず、対象レスが『なぜその行動をとっているのか（背景・心理・AIとの主従逆転・周囲のツッコミ）』を踏まえて、相手の状況そのものへのツッコミや共感を書いてください。",
            "- 周囲のレス（他人のツッコミや判定結果）を『〇〇ってツッコミが来てますけど』のように他人の発言としてメタ実況・報告することは厳禁です。スレッドの空気として内在化し、自分自身のスタンスで語ってください。",
            "- 周囲のレスの単語を適当に繋ぎ合わせたキメラ疑問文（『〜なんですかね？』）は禁止です。相談にはズバッと結論ファーストでアドバイスしてください。",
            "- 【既出ツッコミ・回答の言い換えパクり・キメラ合体厳禁】すでに他のとしあきが書いているツッコミや回答（例: 『もう2割くらい下がってる』『がっつり下がって欲しいが無理だろな』等）を語尾だけ変えて繰り返したり、複数の他人のレスを繋ぎ合わせて自分の発言にすることは【絶対厳禁】です。他人が既に言った内容は避け、まだ出ていない別の視点・理由・リアクションを1つだけ自分の言葉で書いてください。ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。",
        ]
        if excerpt:
            lines.append(f"- 対象レスの本文をそのまま写したら失敗です。対象レス: {excerpt}")
        return "\n".join(lines).strip()

    def _img2chan_comment_parrot_reason(
        self,
        arg1: Any = None,
        arg2: Any = None,
        *,
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        thread: dict[str, Any] | None = None,
    ) -> ParrotReason | str | None:
        if isinstance(arg1, str):
            comment_text = arg1.strip()
            helper = None
            if isinstance(arg2, dict) and thread is None:
                thread = arg2
        else:
            helper = arg1
            comment_text = str(arg2 or "").strip()

        if not comment_text:
            return None

        checker = getattr(helper, "looks_like_img_reply_parrot", None) if helper is not None else None

        def _looks_like_reference_echo(reference_text: object) -> bool:
            reference = str(reference_text or "").strip()
            if not reference:
                return False
            if callable(checker):
                with contextlib.suppress(Exception):
                    if checker(comment_text, reference) is True:
                        return True
            return looks_like_parrot_reply(reference, comment_text)

        # 文頭フレーズ反復（Hook Echo）の検査
        raw_comment = comment_text.strip()
        first_token = re.split(r"[…!！?？、,。\s]", raw_comment)[0].strip()
        if isinstance(target_reply, dict) and len(first_token) >= 3:
            target_txt = str(target_reply.get("text") or "")
            if first_token in target_txt and re.match(rf"^{re.escape(first_token)}[…!！]+", raw_comment):
                if re.search(r"(?:熱さ|懐かし|恥ずかし|青春|時代|あの頃|確かに|分かります|そうですよね|もんね|ですね)", raw_comment):
                    return "hook_echo_abstract_poem"

        if isinstance(target_reply, dict) and _looks_like_reference_echo(target_reply.get("text")):
            return "target_reply"

        for own_post in list(quoted_own_posts or [])[:2]:
            if isinstance(own_post, dict) and _looks_like_reference_echo(own_post.get("text")):
                return "quoted_own_post"

        body = comment_text
        if _SELF_PROJECTION_SYMPATHY_RE.search(body):
            # 対象レスまたは引用元が相談・困りごとであり、かつ具体的な解決ヒント語が含まれていない場合
            ref_texts: list[str] = []
            if isinstance(target_reply, dict):
                ref_texts.append(str(target_reply.get("text") or ""))
            if isinstance(thread, dict):
                for r in (thread.get("replies") or [])[-25:]:
                    if isinstance(r, dict):
                        ref_texts.append(str(r.get("text") or ""))
            if any(_HUTABA_CONSULTATION_RE.search(t) for t in ref_texts):
                # 1文目（共感）を除いた後半に解決策の提案がない場合は自己投影ミラーリングと判定
                lines_no_quote = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.startswith((">", "＞"))]
                non_sympathy_text = " ".join(ln for ln in lines_no_quote if not _SELF_PROJECTION_SYMPATHY_RE.search(ln))
                if not _SOLUTION_HINT_MARKER_RE.search(non_sympathy_text):
                    return "self_projection_sympathy_mirror"

        if _HUTABA_META_SEARCH_REPORT_RE.search(body):
            return "meta_search_failure_report"

        if _HUTABA_META_COMMENTARY_RE.search(body):
            return "meta_commentary_report"

        # 7. スレッド内の他人の既存レス（子レス・周辺レス）の語尾変えパクり・キメラ合体検知
        if isinstance(thread, dict):
            raw_pool = thread.get("all_replies") or thread.get("replies") or []
            if isinstance(raw_pool, list) and raw_pool:
                target_post_no = int((target_reply or {}).get("post_no") or (target_reply or {}).get("number") or 0)
                target_norm_core = (
                    _normalize_for_sibling_plagiarism_check(
                        str((target_reply or {}).get("body") or (target_reply or {}).get("text") or "")
                    )
                    if isinstance(target_reply, dict)
                    else ""
                )
                op_text = str(thread.get("op_text") or thread.get("title") or "")
                op_norm_core = _normalize_for_sibling_plagiarism_check(op_text)
                topic_shared_bigrams: set[str] = set()
                if len(target_norm_core) >= 2:
                    topic_shared_bigrams.update(
                        target_norm_core[i : i + 2] for i in range(len(target_norm_core) - 1)
                    )
                if len(op_norm_core) >= 2:
                    topic_shared_bigrams.update(
                        op_norm_core[i : i + 2] for i in range(len(op_norm_core) - 1)
                    )

                cleaned_comment = _extract_hutaba_non_quote_body(comment_text) or comment_text.strip()
                cand_core = _normalize_for_sibling_plagiarism_check(cleaned_comment)
                c_bigrams = {cand_core[i : i + 2] for i in range(len(cand_core) - 1)} if len(cand_core) >= 2 else set()
                c_unique_bigrams = c_bigrams - topic_shared_bigrams
                matched_sibling_post_nos: list[int] = []
                max_lcs_overall = 0

                seen_pnos: set[int] = set()
                dedup_pool: list[dict[str, Any]] = []
                for rep in raw_pool:
                    if not isinstance(rep, dict) or rep.get("is_op"):
                        continue
                    pno = int(rep.get("post_no") or rep.get("number") or 0)
                    if pno and pno in seen_pnos:
                        continue
                    if pno:
                        seen_pnos.add(pno)
                    dedup_pool.append(rep)

                for rep in dedup_pool:
                    if not isinstance(rep, dict):
                        continue
                    rep_no = int(rep.get("post_no") or rep.get("number") or 0)
                    if target_post_no and rep_no == target_post_no:
                        continue
                    rep_body = str(rep.get("body") or rep.get("text") or "").strip()
                    if not rep_body:
                        continue
                    rep_core = _normalize_for_sibling_plagiarism_check(rep_body)
                    if len(rep_core) < 6:
                        continue

                    # (A) 単一レスからの丸パクリ（6文字以上の既存レスがほぼそのまま含まれる、または8文字以上の共通部分文字列）
                    if len(rep_core) >= 8 and (rep_core in cand_core or cand_core in rep_core):
                        if not (target_norm_core and rep_core in target_norm_core):
                            lcs_val = min(len(cand_core), len(rep_core))
                            return ParrotReason(
                                "sibling_reply_plagiarism",
                                matched_sibling_post_nos=[rep_no],
                                longest_common_subsequence_len=lcs_val,
                            )

                    longest_common = 0
                    for length in range(min(len(cand_core), len(rep_core)), 6, -1):
                        found = False
                        for start in range(0, len(rep_core) - length + 1):
                            sub = rep_core[start : start + length]
                            if sub in cand_core:
                                # 対象レス自体やOPに含まれる共通話題語（固有名詞等）だけの一致なら除外
                                if (target_norm_core and sub in target_norm_core) or (
                                    op_norm_core and sub in op_norm_core
                                ):
                                    continue
                                longest_common = length
                                found = True
                                break
                        if found:
                            break

                    if longest_common > max_lcs_overall:
                        max_lcs_overall = longest_common

                    if longest_common >= 9:
                        return ParrotReason(
                            "sibling_reply_plagiarism",
                            matched_sibling_post_nos=[rep_no],
                            longest_common_subsequence_len=longest_common,
                        )

                    # (B) 複数レスからのつぎはぎ（キメラ合成）検知用の部分一致カウント
                    # 対象レス・OPの共通話題語を除いた固有2-gramで比較し、極短レス（8文字未満）での偶然一致を防ぐ
                    overlap_ratio = 0.0
                    if len(rep_core) >= 8 and len(c_unique_bigrams) >= 3:
                        r_bigrams = {rep_core[i : i + 2] for i in range(len(rep_core) - 1)}
                        r_unique_bigrams = r_bigrams - topic_shared_bigrams
                        if len(r_unique_bigrams) >= 4:
                            common_unique = len(c_unique_bigrams & r_unique_bigrams)
                            overlap_ratio = common_unique / max(1, min(len(c_unique_bigrams), len(r_unique_bigrams)))

                    if longest_common >= 7 or (overlap_ratio >= 0.60 and longest_common >= 5):
                        matched_sibling_post_nos.append(rep_no)

                if len(matched_sibling_post_nos) >= 2:
                    return ParrotReason(
                        "sibling_reply_plagiarism",
                        matched_sibling_post_nos=matched_sibling_post_nos,
                        longest_common_subsequence_len=max_lcs_overall,
                    )

        return None

    async def _maybe_schedule_img2chan_thread_post(self, message: discord.Message) -> None:
        self._ensure_img2chan_proactive_post_task()
        if not cfg_bool("IMG2CHAN_AUTO_POST_ENABLED", True):
            return
        if message.guild is None:
            return
        if getattr(message.author, "bot", False):
            return
        if _get_channel_kind(message) == "none":
            return

        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "_extract_img_thread_urls_full"):
            from front.hutaba.client import img2chan as _img2chan_client
            helper = _img2chan_client
        if helper is None or not hasattr(helper, "_extract_img_thread_urls_full"):
            return

        urls = list(getattr(helper, "_extract_img_thread_urls_full")(content(message)) or [])
        if not urls:
            return

        # 複数指定時は確実に先頭の1件のみを採用
        if len(urls) > 1:
            log.info("[hutaba] 複数スレッドURLを検知。先頭の1件のみを監視対象にします: selected=%s total=%d", urls[0], len(urls))
            urls = urls[:1]

        self._prune_img2chan_post_schedules()
        tasks, recent, states = self._ensure_img2chan_thread_tracking_state()
        now = time.monotonic()
        active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()

        target_raw_url = urls[0]
        normalize = getattr(helper, "normalize_img_thread_url", lambda value: str(value or "").strip())
        url = str(normalize(target_raw_url) or "").strip()
        if not url:
            return

        # 既存アクティブタスクが自発巡回（proactive）の場合は中断してユーザー指定スレッドを最優先
        if active_url and active_url in tasks and not tasks[active_url].done():
            active_state = states.get(active_url, {})
            if not active_state.get("user_requested"):
                log.info("[hutaba] 自発巡回スレッドを中断し、ユーザー指定スレッドへ切り替え: old=%s new=%s", active_url, url)
                tasks[active_url].cancel()
                self._img2chan_active_thread_url = ""
                active_url = ""

        if (
            cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True)
            and active_url
            and url != active_url
            and active_url in tasks
            and not tasks[active_url].done()
        ):
            log.info("[hutaba] アクティブスレッド排他制御によりスキップ url=%s active_url=%s", url, active_url)
            return
        if url in tasks and not tasks[url].done():
            log.info("[hutaba] 投稿タスク既存のためスキップ url=%s", url)
            return

        # ユーザー指定スレッドの初回遅延は短縮（デフォルト5.0秒）
        delay_sec = max(cfg_float("IMG2CHAN_USER_REQUESTED_POST_DELAY_SEC", 5.0), 0.0)
        recent[url] = now
        thread_state = self._img2chan_thread_state(url)
        thread_state["origin_channel_id"] = channel_id(message)
        thread_state["origin_message_id"] = getattr(message, "id", None)
        thread_state["user_requested"] = True
        thread_state["focus_mode"] = True
        self._img2chan_active_thread_url = url
        task = asyncio.create_task(
            self._run_img2chan_thread_post_after_delay(message, url, delay_sec)
        )
        tasks[url] = task

        def _done_callback(done_task: asyncio.Task, *, scheduled_url: str = url) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                exc = done_task.exception()
                if exc is not None:
                    log.exception("[hutaba] スレッド投稿タスク例外発生 url=%s err=%r", scheduled_url, exc)
            task_map = getattr(self, "_img2chan_post_tasks", None)
            if isinstance(task_map, dict) and task_map.get(scheduled_url) is done_task:
                task_map.pop(scheduled_url, None)
            state_map = getattr(self, "_img2chan_thread_states", None)
            if isinstance(state_map, dict):
                state_map.pop(scheduled_url, None)
            active_thread_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
            if active_thread_url == scheduled_url:
                self._img2chan_active_thread_url = ""

        task.add_done_callback(_done_callback)
        log.info(
            "[hutaba] スレッド投稿スケジュール url=%s delay_sec=%.1f channel=%s author=%s",
            url,
            delay_sec,
            channel_id(message),
            author_id(message),
        )

    async def _run_img2chan_thread_post_after_delay(
        self,
        message: discord.Message | None,
        url: str,
        delay_sec: float,
    ) -> None:
        thread_state = self._img2chan_thread_state(url)
        try:
            await asyncio.sleep(max(float(delay_sec), 0.0))
            await self._yield_to_user_replies()
            post_res = await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=delay_sec,
                thread_state=thread_state,
                requester_text=content(message) if message is not None else "",
                report_result=True,
            )
            if isinstance(post_res, dict) and not (post_res.get("posted") or post_res.get("success")) and not thread_state.get("user_requested"):
                log.info(
                    "[hutaba] 自発スレッド初回投稿が見送られたため監視セッションを開始せず終了します: url=%s",
                    url,
                )
                return
            await self._monitor_img2chan_thread_session(message, url, thread_state=thread_state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[hutaba] 遅延投稿処理例外発生 url=%s err=%r", url, e)
            await self._send_img2chan_post_report(
                message,
                {
                    "success": False,
                    "url": url,
                    "comment": "",
                    "error": f"処理中にエラーが発生しました: {e}",
                },
                delay_sec=delay_sec,
            )
        finally:
            reply_tasks = thread_state.get("reply_tasks")
            if isinstance(reply_tasks, dict):
                running_tasks = [task for task in reply_tasks.values() if task is not None and not task.done()]
                for task in running_tasks:
                    task.cancel()
                if running_tasks:
                    await asyncio.gather(*running_tasks, return_exceptions=True)

    async def _select_proactive_thread_candidate(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """ペルソナや興味に基づいてカタログ候補から最適なスレッドを1件選定する。"""
        if not candidates:
            return None

        def _is_cryptic_high_velocity_candidate(cand: dict[str, Any]) -> bool:
            res_cnt = int(cand.get("res_count") or 0)
            if res_cnt < 200:
                return False
            raw_t = str(cand.get("title") or cand.get("op_text") or "").strip()
            core_t = re.sub(r"[\s\u3000、。，．・…‥!！?？「」『』（）()［］\[\]〜~ー―\-━ﾟ∀°]+", "", raw_t)
            return len(core_t) <= 8

        filtered_candidates = [c for c in candidates if not _is_cryptic_high_velocity_candidate(c)]
        effective_candidates = filtered_candidates if filtered_candidates else candidates

        if len(effective_candidates) == 1:
            return effective_candidates[0]

        core_values = str(cfg("BOT_CORE_VALUES", "") or "").strip()
        first_person = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
        persona_context = f"{core_values}".strip()
        if first_person:
            persona_context = f"一人称: {first_person}\n{persona_context}".strip()

        candidate_summaries = []
        for idx, c in enumerate(effective_candidates[:8]):
            title = str(c.get("title") or c.get("text") or "(無題)")[:40]
            res_count = int(c.get("res_count") or 0)
            candidate_summaries.append(f"{idx + 1}. [レス数:{res_count}] {title}")

        board_name = str(effective_candidates[0].get("board") or "img")
        selection_prompt = (
            f"あなたは自分の興味や性格に最も合うふたば☆ちゃんねる({board_name})のスレッドを1つ選びます。\n"
            f"[あなたの人格・興味]\n{persona_context or '日常の雑談、趣味、面白い話題'}\n\n"
            f"[スレッド一覧]\n" + "\n".join(candidate_summaries) + "\n\n"
            "【選定指針】\n"
            "- タイトルやOPが短いセリフだけ（例:『私は先生のことを愛してます』等）でレス数が200件以上進んでいるスレッドは、特定ソシャゲ・版権の専門定時スレである可能性が高いため、自分がよく知らない作品・文脈が掴めないスレは避け、日常雑談や分かりやすいテーマのスレッドを優先して選ぶこと。\n\n"
            "最も参加したいスレッドの番号（1〜）のみを数字だけで答えてください。"
        )

        try:
            await self._yield_to_user_replies()
            model = str(cfg("OLLAMA_MIDDLE_MODEL", cfg("OLLAMA_MODEL", "")) or "").strip()
            if model:
                resp = await self._call_img2chan_ollama_json(
                    selection_prompt,
                    purpose="proactive_thread_selection",
                    model=model,
                    system_prompt="スレッド選定補助AI。JSONで selected_index: 整数 を返してください。",
                    schema={
                        "type": "object",
                        "properties": {"selected_index": {"type": "integer"}},
                        "required": ["selected_index"],
                    },
                    think=False,
                    temperature=0.3,
                    timeout_sec=15.0,
                )
                if isinstance(resp, dict):
                    idx = int(resp.get("selected_index", 1) or 1) - 1
                    if 0 <= idx < len(effective_candidates[:8]):
                        return effective_candidates[idx]
        except Exception as e:
            log.debug("[hutaba] 自発スレ選定LLM失敗、ランダムフォールバック: %r", e)

        return random.choice(effective_candidates[:min(5, len(effective_candidates))])

    def _select_hutaba_patrol_board(self) -> tuple[str, str]:
        """現在の感情状態（気分・好奇心・高揚感・疲労等）に基づいて巡回するふたばサーバー（'img' または 'may'）を決定する。"""
        boards_cfg = cfg("HUTABA_PATROL_BOARDS", "img,may").strip().lower()
        allow_img = "img" in boards_cfg
        allow_may = "may" in boards_cfg
        if allow_img and not allow_may:
            return ("img", "設定によりimg固定")
        if allow_may and not allow_img:
            return ("may", "設定によりmay固定")

        may_prob = cfg_float("HUTABA_MAY_PATROL_BASE_PROB", 0.50)
        reasons: list[str] = []

        emotion_state = getattr(self, "emotion_state", None)
        mood = "neutral"
        dominant = "neutral"
        if emotion_state is not None:
            mood = str(getattr(emotion_state, "mood", "neutral") or "neutral").strip().lower()
            dominant = str(getattr(emotion_state, "dominant_emotion", "neutral") or "neutral").strip().lower()
            raw_scores = getattr(emotion_state, "scores", None)
            scores = raw_scores if isinstance(raw_scores, dict) else {}

            curiosity = float(getattr(emotion_state, "curiosity", scores.get("anticipation", 0.0)) or 0.0)
            excitement = float(getattr(emotion_state, "excitement", scores.get("joy", 0.0)) or 0.0)
            boredom = float(getattr(emotion_state, "boredom", 0.0) or 0.0)
            fatigue = float(getattr(emotion_state, "fatigue", 0.0) or 0.0)
            tension = float(getattr(emotion_state, "tension", scores.get("fear", 0.0)) or 0.0)
            loneliness = float(getattr(emotion_state, "loneliness", scores.get("sadness", 0.0)) or 0.0)

            # 1. 総合ムード（mood）による傾斜
            if mood in ("excited", "upbeat", "curious", "restless", "playful", "energetic"):
                may_prob += 0.20
                reasons.append(f"気分({mood})で賑やか・雑多な話題を求めている")
            elif mood in ("fatigued", "gloomy", "tense", "calm", "sleepy", "melancholy"):
                may_prob -= 0.20
                reasons.append(f"気分({mood})で落ち着いたゆるい空気を求めている")

            # 2. 主要感情（dominant_emotion）による傾斜
            if dominant in ("anticipation", "surprise"):
                may_prob += 0.15
                reasons.append(f"感情({dominant})により刺激・新奇性志向")
            elif dominant == "joy" and excitement >= 0.45:
                may_prob += 0.10
                reasons.append("高揚感がありお祭り気分")
            elif dominant in ("trust", "sadness", "fear"):
                may_prob -= 0.15
                reasons.append(f"感情({dominant})により馴染みのまったり志向")
            elif dominant in ("anger", "disgust"):
                may_prob += 0.10
                reasons.append(f"感情({dominant})の発散に勢いのあるスレ志向")

            # 3. 複合感情インデックスによる傾斜
            if curiosity >= 0.55 or boredom >= 0.55:
                may_prob += 0.12
                reasons.append("好奇心・退屈しのぎに雑多なスレを開拓したい")
            if fatigue >= 0.50 or tension >= 0.50:
                may_prob -= 0.15
                reasons.append("少し疲れ・緊張がありゆるいスレで一息つきたい")
            if loneliness >= 0.55:
                may_prob += 0.08
                reasons.append("人恋しくて流速の速い場所を覗きたい")

        # 4. 連続同一サーバー巡回への飽き補正（気まぐれバランス）
        last_board = str(getattr(self, "_hutaba_last_patrol_board", "") or "").strip().lower()
        streak = int(getattr(self, "_hutaba_patrol_board_streak", 0) or 0)
        if streak >= 2:
            if last_board == "may":
                may_prob -= min(0.25, 0.10 * streak)
                reasons.append("mayが続いたので気分転換も意識")
            elif last_board == "img":
                may_prob += min(0.25, 0.10 * streak)
                reasons.append("imgが続いたので気分転換も意識")

        may_prob = max(0.15, min(0.85, round(may_prob, 3)))
        selected = "may" if random.random() < may_prob else "img"

        setattr(self, "_hutaba_last_patrol_board", selected)
        setattr(self, "_hutaba_patrol_board_streak", (streak + 1) if selected == last_board else 1)

        reason_summary = "・".join(reasons[:2]) if reasons else ("気まぐれで賑やかなmayへ" if selected == "may" else "気まぐれでまったりなimgへ")
        full_reason = f"mood={mood}/dominant={dominant} ({reason_summary}) [may_prob={may_prob:.2f}]"
        return (selected, full_reason)

    def _ensure_img2chan_proactive_post_task(self) -> None:
        """自発スレ巡回ループタスクが未起動なら起動する。"""
        if not self._img2chan_proactive_post_enabled():
            return
        task_post = getattr(self, "_img2chan_proactive_post_task", None)
        if task_post is not None and not task_post.done():
            self._img2chan_proactive_task = task_post
            return
        task_pro = getattr(self, "_img2chan_proactive_task", None)
        if task_pro is not None and not task_pro.done():
            self._img2chan_proactive_post_task = task_pro
            return
        new_task = asyncio.create_task(self._img2chan_proactive_post_loop())
        self._img2chan_proactive_post_task = new_task
        self._img2chan_proactive_task = new_task
        log.info("[hutaba] 自発スレ巡回ループタスクを起動しました")

    async def _img2chan_proactive_post_loop(self) -> None:
        """ふたば（img / may）カタログから定期的にスレッドを自発選定して書き込むループ。"""
        log.info("[hutaba] 自発スレ巡回ループを開始しました")
        initial_delay = max(cfg_float("HUTABA_PROACTIVE_POST_INITIAL_DELAY_SEC", 120.0), 5.0)
        await asyncio.sleep(initial_delay)

        while True:
            try:
                await self._yield_to_user_replies()

                if not self._img2chan_proactive_post_enabled():
                    await asyncio.sleep(60.0)
                    continue

                active_url = str(getattr(self, "_img2chan_active_thread_url", "") or "").strip()
                tasks, recent, states = self._ensure_img2chan_thread_tracking_state()
                active_task = tasks.get(active_url) if active_url else None
                active_state = states.get(active_url, {}) if active_url else {}

                if (
                    (cfg_bool("IMG2CHAN_ACTIVE_THREAD_EXCLUSIVE", True) and active_url and active_task and not active_task.done())
                    or (bool(active_state.get("user_requested")) and active_task and not active_task.done())
                ):
                    log.debug("[hutaba] 既存スレッド監視中（user_requested=%s）のため自発選定を待機: active_url=%s", active_state.get("user_requested", False), active_url)
                    await asyncio.sleep(120.0)
                    continue

                helper = _get_img2chan_helper()
                fetch_catalog = getattr(helper, "fetch_img_top5_threads", None) if helper is not None else None
                if not callable(fetch_catalog):
                    from front.hutaba.client import img2chan as _img2chan_client
                    fetch_catalog = getattr(_img2chan_client, "fetch_img_top5_threads", None)

                if not callable(fetch_catalog):
                    log.warning("[hutaba] カタログ取得関数が利用できないため自発投稿をスキップします")
                    await asyncio.sleep(60.0)
                    continue

                selected_board, board_reason = self._select_hutaba_patrol_board()
                log.info(
                    "[hutaba] 気分に基づく巡回サーバー選定: board=%s (%s)",
                    selected_board,
                    board_reason,
                )

                fallback_board = "img" if selected_board == "may" else "may"
                boards_to_try = [selected_board, fallback_board]
                candidates: list[dict[str, Any]] = []
                active_patrol_board = selected_board

                _, recent, states = self._ensure_img2chan_thread_tracking_state()
                now_mono = time.monotonic()
                dedup_ttl = max(cfg_float("IMG2CHAN_AUTO_POST_DEDUP_TTL_SEC", 3600.0), 60.0)

                for board_idx, current_board in enumerate(boards_to_try):
                    if board_idx > 0:
                        log.info(
                            "[hutaba] %s で有効な候補スレッドが見つからなかったため %s へ巡回先をフォールバックします",
                            selected_board,
                            current_board,
                        )
                    try:
                        catalog_threads = await fetch_catalog(limit=15, timeout_sec=20.0, server=current_board)
                    except TypeError:
                        if board_idx > 0:
                            break
                        catalog_threads = await fetch_catalog(limit=15, timeout_sec=20.0)

                    if not catalog_threads or not isinstance(catalog_threads, list):
                        log.info("[hutaba] カタログ取得結果が空でした (board=%s)", current_board)
                        continue

                    board_candidates: list[dict[str, Any]] = []
                    for item in catalog_threads:
                        if not isinstance(item, dict):
                            continue
                        raw_url = str(item.get("url") or "").strip()
                        t_url = normalize_img_thread_url(raw_url)
                        if not t_url:
                            continue
                        if self._is_img2chan_thread_vandalized(t_url):
                            continue
                        stream_reason = self._is_img2chan_stream_or_live_thread(item)
                        if stream_reason:
                            log.info(
                                "[hutaba] 配信・実況系スレッドのため自発投稿対象から除外: %s (reason=%s title=%s)",
                                t_url,
                                stream_reason,
                                str(item.get("title") or "")[:40],
                            )
                            continue
                        request_reason = self._is_img2chan_request_thread(item)
                        if request_reason:
                            log.info(
                                "[hutaba] リクエスト系スレッドのため自発投稿対象から除外: %s (reason=%s title=%s)",
                                t_url,
                                request_reason,
                                str(item.get("title") or item.get("text") or "")[:40],
                            )
                            continue
                        expire_reason = self._is_hutaba_thread_expiring_soon(item)
                        if expire_reason:
                            log.info(
                                "[hutaba] 消滅間近スレッド（10分以内等）のため自発投稿対象から除外: %s (reason=%s expire_text=%s title=%s)",
                                t_url,
                                expire_reason,
                                str(item.get("expire_text") or ""),
                                str(item.get("title") or "")[:40],
                            )
                            continue
                        res_count = int(item.get("res_count") or item.get("reply_count") or 0)
                        max_proactive_res = int(cfg("HUTABA_MAX_PROACTIVE_THREAD_RES_COUNT", 650) or 650)
                        if res_count > max_proactive_res:
                            log.debug(
                                "[hutaba] 終盤スレッド（レス数:%d > %d）のため自発投稿対象から除外: %s",
                                res_count,
                                max_proactive_res,
                                t_url,
                            )
                            continue
                        if res_count > 850:
                            continue
                        text = str(item.get("text") or "").strip()
                        if not text or text in {"(取得失敗)", "(エラー)"}:
                            continue
                        last_posted = float(recent.get(t_url, 0.0) or 0.0)
                        if last_posted and (now_mono - last_posted) < dedup_ttl:
                            continue
                        st = states.get(t_url)
                        if isinstance(st, dict) and st.get("status") in ("aborted", "completed"):
                            continue
                        item_copy = dict(item)
                        item_copy["normalized_url"] = t_url
                        item_copy["board"] = str(item.get("board") or current_board)
                        board_candidates.append(item_copy)

                    if board_candidates:
                        candidates = board_candidates
                        active_patrol_board = current_board
                        break

                if not candidates:
                    log.info("[hutaba] 条件を満たす新規巡回候補スレッドがありませんでした")
                    continue

                log.info(
                    "[hutaba] 自発スレッド選定開始: 候補数=%d (board=%s)",
                    len(candidates),
                    active_patrol_board,
                )
                await self._wait_for_discord_user_replies_idle()
                selected = await self._select_proactive_thread_candidate(candidates)
                if selected:
                    selected_url = str(selected.get("normalized_url") or selected.get("url") or "").strip()
                    selected_title = str(selected.get("title") or selected.get("text") or "")[:30]
                    log.info("[hutaba] 自発スレッド選定完了: url=%s title=%s", selected_url, selected_title)

                    recent[selected_url] = now_mono
                    thread_state = self._img2chan_thread_state(selected_url)
                    thread_state["board"] = active_patrol_board
                    self._img2chan_active_thread_url = selected_url

                    task = asyncio.create_task(
                        self._run_img2chan_thread_post_after_delay(None, selected_url, delay_sec=0.0)
                    )
                    tasks[selected_url] = task

                    def _done_cb(done_task: asyncio.Task, *, s_url: str = selected_url) -> None:
                        with contextlib.suppress(asyncio.CancelledError):
                            exc = done_task.exception()
                            if exc is not None:
                                log.exception("[hutaba] 自発投稿タスク例外発生 url=%s err=%r", s_url, exc)
                        task_map = getattr(self, "_img2chan_post_tasks", None)
                        if isinstance(task_map, dict) and task_map.get(s_url) is done_task:
                            task_map.pop(s_url, None)
                        if getattr(self, "_img2chan_active_thread_url", "") == s_url:
                            self._img2chan_active_thread_url = ""

                    task.add_done_callback(_done_cb)

                interval_min = self._img2chan_proactive_post_interval_min_sec()
                interval_max = self._img2chan_proactive_post_interval_max_sec()
                sleep_duration = random.uniform(interval_min, interval_max)
                log.info("[hutaba] 次の自発巡回まで待機: %.1f秒", sleep_duration)
                await asyncio.sleep(sleep_duration)

            except asyncio.CancelledError:
                log.info("[hutaba] 自発スレ巡回ループがキャンセルされました")
                raise
            except Exception as e:
                log.exception("[hutaba] 自発スレ巡回ループでエラー発生: %r", e)
                await asyncio.sleep(60.0)

    def _select_img2chan_chitchat_target(
        self,
        thread: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """相槌・追撃レスの対象として、既に他者から返信をもらっているレスを最優先で選定する。"""
        if not candidates:
            return None

        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        # 既に誰かから返信をもらっているレス（被引用数 > 0）を抽出
        replied_candidates: list[tuple[int, int, dict[str, Any]]] = []
        for idx, cand in enumerate(candidates):
            if _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                continue
            p_no = str(cand.get("post_no") or "").strip()
            r_num = str(int(cand.get("number") or 0))
            raw_cnt = reply_counts.get(p_no, 0) or reply_counts.get(r_num, 0)
            cnt = int(raw_cnt) if isinstance(raw_cnt, (int, float)) else 0
            if cnt > 0:
                # 被返信数降順、かつ同数なら新着順（idx昇順）
                replied_candidates.append((-cnt, idx, cand))

        if replied_candidates:
            replied_candidates.sort(key=lambda x: (x[0], x[1]))
            selected = replied_candidates[0][2]
            log.info(
                "[hutaba] 既に返信されているホットなレスを相槌対象に選定: post_no=%s quoted_count=%d",
                selected.get("post_no"),
                -replied_candidates[0][0],
            )
            return selected

        # 返信をもらっているレスがない場合は最新レスにフォールバック（動画レスは除外）
        for cand in candidates:
            if not _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                return cand
        return None

    async def _maybe_schedule_img2chan_autonomous_chitchat(
        self,
        message: discord.Message | None,
        url: str,
        thread_state: dict[str, Any],
        thread: dict[str, Any],
    ) -> None:
        """引用返信がなくても、他人の会話が新しく進行している場合にのみ相槌・追撃レスを検討する。"""
        if not cfg_bool("HUTABA_AUTONOMOUS_CHITCHAT_ENABLED", False):
            # デフォルトは無効または慎重運用とし、不要な自己連投リスクを遮断
            return

        now = time.time()
        last_own_post = float(thread_state.get("last_own_post_at", 0.0) or 0.0)
        if thread_state.get("user_requested"):
            cooldown = max(cfg_float("IMG2CHAN_USER_REQUESTED_AUTONOMOUS_FOLLOWUP_COOLDOWN_SEC", 120.0), 10.0)
        else:
            cooldown = self._img2chan_autonomous_followup_cooldown_sec()
        if last_own_post > 0 and (now - last_own_post) < cooldown:
            return

        replies = list((thread or {}).get("replies") or [])
        if len(replies) < 3:
            return

        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set(thread_state.get("own_post_texts", {}).values())

        # 【超重要ガード1】スレッドの最新レス（末尾）が自分自身の場合は絶対に連投しない
        latest_reply = replies[-1]
        latest_post_no = str(latest_reply.get("post_no") or "").strip()
        latest_text = str(latest_reply.get("text") or "").strip()
        if latest_post_no in own_nos or latest_text in own_texts:
            log.debug("[hutaba] スレ最新レスが自身のため追撃を抑止（他人のレス待ち）: url=%s", url)
            return

        # 【超重要ガード2】最後の自身の投稿より後に投稿された他人のレスのみを対象とする
        new_other_replies = []
        found_own_post = False
        for r in reversed(replies):
            p_no = str(r.get("post_no") or "").strip()
            p_txt = str(r.get("text") or "").strip()
            if p_no in own_nos or p_txt in own_texts:
                found_own_post = True
                break
            new_other_replies.append(r)

        # 自身の書き込み履歴があるのに、その後に他人の新着レスが1件もない場合は即座にリターン
        if found_own_post and not new_other_replies:
            return

        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        # 既に誰かの返信がついているレス（被引用数 > 0）のみを抽出
        candidate_pool = new_other_replies if new_other_replies else [replies[-1]]
        replied_candidates: list[dict[str, Any]] = []
        for cand in candidate_pool:
            if _has_hutaba_unsupported_media(str(cand.get("text") or "")):
                continue
            p_no = str(cand.get("post_no") or "").strip()
            r_num = str(int(cand.get("number") or 0))
            if reply_counts.get(p_no, 0) > 0 or reply_counts.get(r_num, 0) > 0:
                replied_candidates.append(cand)

        # 誰の返信もついていないレスしかない場合は、特定レスへの返信は行わない（スキップ）
        if not replied_candidates:
            log.debug("[hutaba] 既に返信がついているレスが存在しないため相槌タスクを見送り: url=%s", url)
            return

        target_reply = self._select_img2chan_chitchat_target(thread, replied_candidates) or replied_candidates[0]
        if target_reply is None:
            return

        target_post_no = str(target_reply.get("post_no") or "").strip()
        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight

        if not target_post_no or target_post_no in own_nos or target_post_no in handled or target_post_no in inflight:
            return

        reply_tasks = thread_state.get("reply_tasks")
        if isinstance(reply_tasks, dict) and len(reply_tasks) == 0:
            inflight.add(target_post_no)
            reply_tasks[target_post_no] = asyncio.create_task(
                self._run_img2chan_followup_reply_task(
                    message,
                    url,
                    thread_state=thread_state,
                    target_reply=target_reply,
                    quoted_own_posts=[],
                    target_replies=replies,
                )
            )
            log.info("[hutaba] スレ進行に応じた自発的相槌タスクをスケジュール url=%s target_post_no=%s", url, target_post_no)

    async def _monitor_img2chan_thread_session(
        self,
        message: discord.Message | None,
        url: str,
        *,
        thread_state: dict[str, Any],
    ) -> None:
        helper = _get_img2chan_helper()
        if helper is None or not hasattr(helper, "fetch_img_thread"):
            return
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        max_replies = self._img2chan_thread_monitor_max_replies()
        if thread_state.get("user_requested"):
            poll_sec = max(cfg_float("IMG2CHAN_USER_REQUESTED_POLL_SEC", 15.0), 5.0)
        else:
            poll_sec = self._img2chan_thread_monitor_poll_sec()

        while True:
            try:
                await self._yield_to_user_replies()
                cached_caption = str(thread_state.get("op_image_caption") or "").strip() or None
                try:
                    thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=max_replies,
                        timeout_sec=timeout_sec,
                        cached_caption=cached_caption,
                    )
                except TypeError:
                    thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=max_replies,
                        timeout_sec=timeout_sec,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[hutaba] スレッド監視フェッチ失敗 url=%s err=%r", url, e)
                await asyncio.sleep(poll_sec)
                continue

            error = str((thread or {}).get("error") or "").strip()
            if error:
                if self._img2chan_thread_dropped(error):
                    thread_state["status"] = "completed"
                    log.info("[hutaba] スレッド完走・消滅を検知（セッション正常終了）: url=%s reason=%s", url, error)
                    if getattr(self, "_img2chan_active_thread_url", "") == url:
                        self._img2chan_active_thread_url = ""
                    return
                log.warning("[hutaba] スレッド監視一時的取得エラー url=%s err=%s", url, error)
                await asyncio.sleep(poll_sec)
                continue

            if thread and isinstance(thread, dict):
                caption = str(thread.get("op_image_caption") or "").strip()
                if caption and not thread_state.get("op_image_caption"):
                    thread_state["op_image_caption"] = caption

            # 荒らし・不毛スレ検知（Vandalism Detection）による早期撤退
            vandalism_reason = self._detect_img2chan_thread_vandalism(thread, thread_state)
            if vandalism_reason:
                thread_state["status"] = "aborted"
                thread_state["abort_reason"] = f"vandalism_detected: {vandalism_reason}"
                log.warning(
                    "[hutaba] 荒らし・不毛スレ化を検知したため書き込み・監視を中止（撤退）: url=%s reason=%s",
                    url,
                    vandalism_reason,
                )
                self._mark_img2chan_thread_vandalized(url, vandalism_reason)
                reply_tasks = thread_state.get("reply_tasks")
                if isinstance(reply_tasks, dict):
                    for task in list(reply_tasks.values()):
                        if task is not None and not task.done():
                            task.cancel()
                if getattr(self, "_img2chan_active_thread_url", "") == url:
                    self._img2chan_active_thread_url = ""
                return

            self._match_img2chan_pending_own_posts(thread_state, thread)
            thread_state["target_replies"] = list((thread or {}).get("replies") or [])
            candidates = self._discover_img2chan_quote_reply_candidates(thread_state, thread)
            if candidates:
                self._schedule_img2chan_followup_reply_tasks(message, url, thread_state, candidates)
            else:
                await self._maybe_schedule_img2chan_autonomous_chitchat(message, url, thread_state, thread)

            await self._maybe_learn_from_img2chan_thread_feedback(message, url, thread_state, thread)
            await asyncio.sleep(poll_sec)

    async def _run_img2chan_followup_reply_task(
        self,
        message: discord.Message | None,
        url: str,
        *,
        thread_state: dict[str, Any],
        target_reply: dict[str, Any],
        quoted_own_posts: list[dict[str, Any]],
        target_replies: list[dict[str, Any]] | None = None,
    ) -> None:
        target_post_no = str(target_reply.get("post_no") or "").strip()
        target_text = str(target_reply.get("text") or "").strip()
        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set(thread_state.get("own_post_texts", {}).values())

        # 自分自身のレスへの返信は絶対に実行しない
        if target_post_no in own_nos or target_text in own_texts:
            log.warning("[hutaba] 自身のレスへの自己返信を検知したためタスクを破棄: url=%s target_post_no=%s", url, target_post_no)
            return

        raw_handled = thread_state.get("handled_quote_reply_post_nos")
        handled: set[Any] = raw_handled if isinstance(raw_handled, set) else set()
        thread_state["handled_quote_reply_post_nos"] = handled
        raw_inflight = thread_state.get("inflight_quote_reply_post_nos")
        inflight: set[Any] = raw_inflight if isinstance(raw_inflight, set) else set()
        thread_state["inflight_quote_reply_post_nos"] = inflight
        raw_tasks = thread_state.get("reply_tasks")
        reply_tasks: dict[Any, Any] = raw_tasks if isinstance(raw_tasks, dict) else {}
        thread_state["reply_tasks"] = reply_tasks

        try:
            # 直近数件のレスを検査
            if target_replies is None:
                raw_target_replies = thread_state.get("target_replies")
                if isinstance(raw_target_replies, list):
                    target_replies = raw_target_replies
                else:
                    target_replies = [target_reply] if target_reply else []

            recent_comments = [str(r.get("com", "") or r.get("text", "")) for r in target_replies[-5:]]
            denial_hits = sum(1 for c in recent_comments if _looks_like_hutaba_anger_or_denial(c))
            if denial_hits >= 2:
                log.info(
                    "[HUTABA_FRUSTRATION] Denial/anger detected in thread %s (hits=%d). Skipping reply cycle for cooldown.",
                    url,
                    denial_hits,
                )
                return

            # 中断・再開用のチェックポイント保存
            self._save_img2chan_checkpoint(
                thread_state,
                target_post_no=target_post_no,
                action="followup_reply",
                url=url,
            )

            # Discordユーザー返信最優先
            await self._yield_to_user_replies()

            # 中断から復帰したか確認
            checkpoint = self._restore_img2chan_checkpoint(thread_state)
            if checkpoint and checkpoint.get("target_post_no") == target_post_no:
                log.info(
                    "[hutaba] Discord返信完了を検知。中断されていたスレ書き込み処理を再開: url=%s target=%s",
                    url,
                    target_post_no,
                )
                self._clear_img2chan_checkpoint(thread_state)

            result = await self._execute_img2chan_thread_post(
                message,
                url,
                delay_sec=None,
                thread_state=thread_state,
                requester_text="",
                target_reply=target_reply,
                quoted_own_posts=quoted_own_posts,
                report_result=True,
            )
            if bool(result.get("success")):
                handled.add(target_post_no)
                thread_state["last_own_post_at"] = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[hutaba] 引用返信タスク例外発生 url=%s target_post_no=%s err=%r", url, target_post_no, e)
        finally:
            inflight.discard(target_post_no)
            if reply_tasks.get(target_post_no) is asyncio.current_task():
                reply_tasks.pop(target_post_no, None)

    def _extract_early_thread_context_lines(
        self,
        posts: list[dict[str, Any]],
        *,
        max_lines: int = 6,
    ) -> list[str]:
        """スレッドが長大化（len(posts) > 8）した際に、序盤〜中盤（posts[:-8]）から
        重要な文脈（OP、疑問、引用、主張・宣言、そうだね獲得レスなど）を最大 max_lines 件抽出して整形する。
        """
        if not posts or len(posts) <= 8 or max_lines <= 0:
            return []

        result_lines: list[str] = []

        # 1. OP（posts[0]）はスレッドの前提であるため必ず先頭に含める
        op_post = posts[0]
        op_text = str(op_post.get("text") or "").strip()
        if op_text:
            op_no = str(op_post.get("post_no") or op_post.get("number") or "").strip()
            op_body = " ".join(op_text.split())
            op_line = f"No.{op_no}: {truncate_text(op_body, 120)}" if op_no else f"OP: {truncate_text(op_body, 120)}"
            result_lines.append(op_line)

        available_slots = max_lines - len(result_lines)
        if available_slots <= 0:
            return result_lines

        # 2. 序盤〜中盤のレス（posts[1:-8]）から候補を抽出
        early_candidates = posts[1:-8]
        valid_candidates: list[tuple[int, dict[str, Any], int]] = []

        for idx, post in enumerate(early_candidates):
            text = str(post.get("text") or "").strip()
            if not text:
                continue

            score = 0
            soudane = int(post.get("soudane") or 0)
            if soudane > 0:
                score += min(soudane, 10) * 2

            # 疑問符・感嘆符
            if any(punct in text for punct in ("?", "？", "!", "！")):
                score += 2

            # 引用行を含む
            if any(line.strip().startswith(">") or line.strip().startswith("＞") for line in text.splitlines()):
                score += 2

            # 主張・宣言・特徴的な表現
            if any(term in text for term in ("思う", "はず", "だろ", "じゃん", "宣言", "結論", "わけ", "本当", "実は")):
                score += 1

            valid_candidates.append((idx, post, score))

        if not valid_candidates:
            return result_lines

        if len(valid_candidates) <= available_slots:
            selected = valid_candidates
        else:
            scored_candidates = [c for c in valid_candidates if c[2] > 0]
            scored_candidates.sort(key=lambda x: x[2], reverse=True)
            chosen_indices: set[int] = {c[0] for c in scored_candidates[:available_slots]}

            if len(chosen_indices) < available_slots:
                needed = available_slots - len(chosen_indices)
                unselected = [c for c in valid_candidates if c[0] not in chosen_indices]
                if unselected:
                    step = len(unselected) / needed
                    sampled = [unselected[int(i * step)] for i in range(needed)]
                    for s in sampled:
                        chosen_indices.add(s[0])

            selected = [c for c in valid_candidates if c[0] in chosen_indices]

        for _, post, _ in selected:
            post_no = str(post.get("post_no") or post.get("number") or "").strip()
            body = " ".join(str(post.get("text") or "").split())
            if not body:
                continue
            line = f"No.{post_no}: {truncate_text(body, 120)}" if post_no else truncate_text(body, 120)
            result_lines.append(line)
            if len(result_lines) >= max_lines:
                break

        return result_lines

    def _extract_hutaba_target_context_lines(
        self,
        thread: dict[str, Any],
        target_reply: dict[str, Any],
        posts: list[dict[str, Any]],
        *,
        max_lines: int = 6,
    ) -> list[str]:
        """対象レスの直前レス、対象レスへの返信（他人のツッコミ）、
        および対象レス内の重要語を含む過去言及レス（ローカルミーム）を抽出する。
        """
        if not target_reply or not posts:
            return []
        target_p_no = _normalize_img_post_no(target_reply.get("post_no"))
        target_r_no = int(target_reply.get("number") or 0)
        target_text = str(target_reply.get("text") or "").strip()
        # 引用されていない対象レス自身の本文先頭行（本文引用判定用）
        target_plain_lines = [
            line.strip() for line in target_text.splitlines()
            if line.strip() and not line.strip().startswith(">")
        ]
        target_first_line = target_plain_lines[0][:24] if target_plain_lines else ""

        context_lines: list[str] = []
        seen_keys: set[str] = {target_p_no} if target_p_no else set()

        # 対象レスのインデックスを特定
        target_idx = -1
        for idx, p in enumerate(posts):
            if _normalize_img_post_no(p.get("post_no")) == target_p_no or (
                target_r_no > 0 and int(p.get("number") or 0) == target_r_no
            ):
                target_idx = idx
                break

        # 1. 対象レスへの返信（子レス / 既についた他人のツッコミ）の抽出
        find_quoted_fn = getattr(_get_img2chan_helper(), "find_img_thread_quoted_posts", None)
        search_replies = posts[target_idx + 1 :] if target_idx >= 0 else posts
        for p in search_replies:
            p_no = _normalize_img_post_no(p.get("post_no"))
            if not p_no or p_no in seen_keys:
                continue
            text = str(p.get("text") or "").strip()
            all_quote_lines = [
                line.strip().lstrip(">").strip()
                for line in text.splitlines()
                if line.strip().startswith(">")
            ]
            direct_quote_lines = [
                line.strip()[1:].strip()
                for line in text.splitlines()
                if line.strip().startswith(">") and not line.strip().startswith(">>")
            ]
            is_replying = False
            if target_p_no and any(q == target_p_no or q == f"No.{target_p_no}" for q in all_quote_lines):
                is_replying = True
            elif target_r_no > 0 and any(q == str(target_r_no) for q in all_quote_lines):
                is_replying = True
            elif len(target_first_line) >= 4 and any(target_first_line in q for q in direct_quote_lines):
                is_replying = True
            elif callable(find_quoted_fn) and not direct_quote_lines:
                with contextlib.suppress(Exception):
                    parents = find_quoted_fn(thread, p, max_targets=1)
                    if parents and _normalize_img_post_no(parents[0].get("post_no")) == target_p_no:
                        is_replying = True

            if is_replying:
                r_num = p.get("number") or 0
                header = (
                    f">>{r_num} No.{p_no} [対象レスへの他人のツッコミ(重複・言い換え合体厳禁)]"
                    if r_num
                    else f"No.{p_no} [対象レスへの他人のツッコミ(重複・言い換え合体厳禁)]"
                )
                context_lines.append(f"{header}: {truncate_text(text, 100)}")
                seen_keys.add(p_no)
                if len(context_lines) >= 2:
                    break

        terms = [t for t in _extract_hutaba_named_terms(target_text) if len(t) >= 2 and t not in ("俺は", "いい")]
        terms_lower = [t.lower() for t in terms]
        target_has_quote = any(line.strip().startswith((">", "＞")) for line in target_text.splitlines())

        # 2. 対象レスの直前会話スライス（先行レス群 / 親文脈）の抽出
        # 対象レスが直近10件に含まれない場合、直前1〜3レスを遡って先行する話題の流れを確保
        # ※ただし対象レスに引用（>）がなく独自の重要語を持つ場合、重要語が一切重ならない無関係な単発レスは除外する
        if target_idx > 0 and target_idx < len(posts) - 10:
            slice_start = max(0, target_idx - 3)
            for prev_p in posts[slice_start:target_idx]:
                prev_p_no = _normalize_img_post_no(prev_p.get("post_no"))
                if prev_p_no and prev_p_no not in seen_keys and not prev_p.get("is_op"):
                    prev_text = str(prev_p.get("text") or "").strip()
                    if not prev_text:
                        continue
                    is_immediate_prev = (prev_p == posts[target_idx - 1])
                    if not target_has_quote and terms_lower and not is_immediate_prev:
                        prev_terms_lower = {
                            t.lower() for t in _extract_hutaba_named_terms(prev_text)
                            if _KATAKANA_TERM_RE.search(t) or _ALPHANUM_TERM_RE.search(t) or t.lower() in _HUTABA_TERM_SYNONYM_MAP
                        }
                        prev_text_lower = prev_text.lower()
                        # 直前2件以上前のレスが別の固有名詞を持ち、かつ対象レスの重要語（同義語含む）を1つも含まない場合は無関係な別話題としてスキップ
                        if prev_terms_lower and not any(tl in prev_text_lower for tl in terms_lower) and not ( set(terms_lower) & prev_terms_lower ):
                            continue
                    prev_num = prev_p.get("number") or 0
                    header = f">>{prev_num} No.{prev_p_no} [直前の会話・先行レス]" if prev_num else f"No.{prev_p_no} [直前の会話・先行レス]"
                    context_lines.append(f"{header}: {truncate_text(prev_text, 120)}")
                    seen_keys.add(prev_p_no)

        # 3. 対象レス内の重要語（同義語・略称含む）によるスレッド過去言及（ローカルミーム・先行話題）の抽出
        # 対象レスより前のレスを新しい順（target_idx - 1 から逆順）に走査し、同じ話題の先行レスを優先抽出する
        if terms_lower and len(context_lines) < max_lines:
            prior_pool = list(reversed(posts[:target_idx])) if target_idx > 0 else posts[: max(0, len(posts) - 10)]
            for p in prior_pool:
                p_no = _normalize_img_post_no(p.get("post_no"))
                if not p_no or p_no in seen_keys or p.get("is_op"):
                    continue
                p_txt = str(p.get("text") or "").strip()
                p_txt_lower = unicodedata.normalize("NFKC", p_txt).lower()
                if any(tl in p_txt_lower for tl in terms_lower):
                    p_num = p.get("number") or 0
                    header = f">>{p_num} No.{p_no} [スレッド内関連レス]" if p_num else f"No.{p_no} [スレッド内関連レス]"
                    context_lines.append(f"{header}: {truncate_text(p_txt, 100)}")
                    seen_keys.add(p_no)
                    if len(context_lines) >= max_lines:
                        break

        return context_lines[:max_lines]

    def _infer_fallback_quote_post_no(
        self,
        thread: dict[str, Any],
        comment: str,
        thread_state: dict[str, Any] | None = None,
    ) -> str:
        """進行中スレッドでLLMが quote_post_numbers を空で返した場合に、
        直近10レスから comment と最も語彙重複度の高いレスの post_no を推定する。
        """
        comment_text = str(comment or "").strip()
        if not comment_text:
            return ""
        replies = [r for r in list(thread.get("replies") or []) if isinstance(r, dict)]
        total_replies = int(thread.get("total_reply_count") or len(replies))
        if total_replies < 10 and len(replies) < 10:
            return ""

        own_nos = set((thread_state or {}).get("own_post_nos") or set())
        own_texts = set(((thread_state or {}).get("own_post_texts") or {}).values())

        token_pattern = re.compile(r"[一-龠々]{2,}|[ァ-ヴー]{2,}|[A-Za-z0-9]{2,}|[ぁ-ん]{3,}")
        stopwords = {"これ", "それ", "あれ", "ここ", "そこ", "ほんと", "本当", "みたい", "感じ", "ちゃん", "さん", "です", "ます", "ました", "思っ", "ちゃい"}
        comment_tokens = {
            tok.lower()
            for tok in token_pattern.findall(comment_text)
            if tok.lower() not in stopwords
        }
        if not comment_tokens:
            return ""

        best_post_no = ""
        best_score = 0
        helper = _get_img2chan_helper()
        counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
        raw_counts = counter_fn(thread) if callable(counter_fn) else {}
        reply_counts: dict[str, int] = raw_counts if isinstance(raw_counts, dict) else {}

        for idx, reply in enumerate(replies[-10:]):
            p_no = str(reply.get("post_no") or "").strip()
            r_num = str(int(reply.get("number") or 0))
            p_text = str(reply.get("text") or "").strip()
            if not p_no or not p_text or p_no in own_nos or p_text in own_texts:
                continue

            # 誰の返信もついていないレスは引用補完の対象外（返信しないタイプのレス生成を維持）
            if reply_counts.get(p_no, 0) <= 0 and reply_counts.get(r_num, 0) <= 0:
                continue

            reply_tokens = {tok.lower() for tok in token_pattern.findall(p_text)}
            overlap = sum(len(tok) * 2 for tok in (comment_tokens & reply_tokens))
            for tok in comment_tokens:
                if tok not in reply_tokens and len(tok) >= 2 and tok in p_text.lower():
                    overlap += len(tok)
            for r_tok in reply_tokens:
                if r_tok not in comment_tokens and len(r_tok) >= 2 and r_tok in comment_text.lower():
                    overlap += len(r_tok)
            if overlap > 0:
                # 被返信数ボーナス（1件あたり15点加算）
                raw_bonus = reply_counts.get(p_no, 0) or reply_counts.get(r_num, 0)
                quoted_bonus = int(raw_bonus) * 15 if isinstance(raw_bonus, (int, float)) else 0
                score = overlap * 10 + quoted_bonus + idx
                if score > best_score:
                    best_score = score
                    best_post_no = p_no
        return best_post_no

    def _resolve_img2chan_quoted_target_reply(
        self,
        thread: dict[str, Any],
        quote_post_numbers: list[str] | None = None,
        quote_reply_numbers: list[int] | None = None,
    ) -> dict[str, Any] | None:
        """quote_post_numbers または quote_reply_numbers に対応するスレッド内のレス辞書を返す。"""
        q_posts = [str(p or "").strip() for p in list(quote_post_numbers or []) if str(p or "").strip()]
        q_replies = [int(r) for r in list(quote_reply_numbers or []) if isinstance(r, (int, str)) and str(r).isdigit() and int(r) > 0]
        if not q_posts and not q_replies:
            return None

        candidates = [
            *list(thread.get("replies") or []),
            *list(thread.get("posts") or []),
        ]
        for post in candidates:
            if not isinstance(post, dict):
                continue
            p_no = str(post.get("post_no") or "").strip()
            norm_p_no = _normalize_img_post_no(p_no)
            r_no = int(post.get("number") or 0)
            if p_no and (p_no in q_posts or norm_p_no in q_posts or f"No.{norm_p_no}" in q_posts):
                return post
            if r_no > 0 and (r_no in q_replies or str(r_no) in q_posts):
                return post
        return None

    def _extract_img2chan_macro_topic_summary(
        self,
        thread: dict[str, Any],
        posts: list[dict[str, Any]],
    ) -> str:
        """スレッド全域から序盤の話題と中盤〜直近の支配的トピックを抽出し、話題遷移サマリーを生成する。"""
        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        if not all_posts or len(all_posts) < 15:
            return ""

        op_text = str(thread.get("op_text") or "").strip()
        from collections import Counter

        # 序盤 (1/3) と 中盤〜終盤 (2/3) の単語頻出度を比較
        split_idx = max(len(all_posts) // 3, 5)
        early_posts = all_posts[:split_idx]
        later_posts = all_posts[split_idx:]

        early_counter: Counter[str] = Counter()
        for p in early_posts:
            for term in _extract_hutaba_named_terms(str(p.get("text") or "")):
                early_counter[term] += 1

        later_counter: Counter[str] = Counter()
        for p in later_posts:
            for term in _extract_hutaba_named_terms(str(p.get("text") or "")):
                later_counter[term] += 1

        early_top = [t for t, _ in early_counter.most_common(3)]
        later_top = [t for t, _ in later_counter.most_common(4) if t not in early_top[:1]]

        # スレ内全域での反復フレーズ（定型ツッコミ・ミーム）および注目固有名詞・カタカナ語の汎用抽出
        phrase_counter: Counter[str] = Counter()
        entity_counter: Counter[str] = Counter()

        for p in all_posts:
            p_text = str(p.get("text") or "").strip()
            if not p_text:
                continue

            # レスごとの重複を避けるための集合
            post_entities: set[str] = set()
            for term in _extract_hutaba_named_terms(p_text):
                # カタカナ語（2〜16文字）または英数字名（2〜20文字）等の固有名詞・注目対象
                if (
                    re.fullmatch(r"[ァ-ヶー]{2,16}", term)
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{1,19}", term)
                ):
                    if term not in _HUTABA_TOPIC_STOPWORDS:
                        post_entities.add(term)
            for ent in post_entities:
                entity_counter[ent] += 1

            # 行単位および引用単位の短文フレーズ（定型ツッコミ・反復ミーム）
            post_phrases: set[str] = set()
            for raw_line in p_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                clean_line = re.sub(r"^[>＞\s]+", "", line).strip()
                clean_line = re.sub(r"^>>\d+\s*", "", clean_line).strip()
                clean_line = re.sub(r"https?://\S+", "", clean_line).strip()
                clean_line = clean_line.strip("!！?？…・wWｗ \t。、")
                if (
                    len(clean_line) < 3
                    or len(clean_line) > 25
                    or re.fullmatch(r"^[0-9\W_]+$", clean_line)
                    or _HUTABA_FILENAME_TOKEN_RE.match(clean_line)
                    or clean_line in _HUTABA_TOPIC_STOPWORDS
                ):
                    continue
                post_phrases.add(clean_line)
            for phr in post_phrases:
                phrase_counter[phr] += 1

        recurring_entities = [
            ent for ent, cnt in entity_counter.most_common(4)
            if cnt >= 2
        ]
        recurring_phrases = [
            phr for phr, cnt in phrase_counter.most_common(3)
            if cnt >= 2 and phr not in recurring_entities
        ]

        lines = []
        if op_text:
            lines.append(f"- スレッド初期の主題: {op_text[:40]}")
        elif early_top:
            lines.append(f"- スレッド初期の主な話題: {', '.join(early_top)}")

        if later_top:
            lines.append(f"- 中盤〜現在の盛り上がりトピック: {', '.join(later_top)} (※話題がシフトしている場合は現在の流れに合わせること)")

        if recurring_entities:
            lines.append(f"- スレ内の注目固有名詞・いじり対象: {', '.join(recurring_entities)} (※話題の中心や風刺・関心の対象)")

        if recurring_phrases:
            phrase_str = ", ".join(f"「{p}」" for p in recurring_phrases)
            lines.append(f"- スレ内の反復フレーズ・定型ツッコミ: {phrase_str} (※スレ特有のノリ・ミームの文脈を踏まえ、野暮なマジレスを避けること)")

        return "\n".join(lines)

    async def _resolve_hutaba_thread_topic_context(
        self,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
        *,
        target_reply: dict[str, Any] | None = None,
        posts: list[dict[str, Any]] | None = None,
    ) -> str:
        """スレッド内の固有名詞・主題および返信対象レスの未知語を事前Web調査し、プロンプト用要約メモを生成・キャッシュする。"""
        researched_terms: list[str] = thread_state.setdefault("researched_terms", [])
        research_count: int = int(thread_state.setdefault("topic_research_count", 0))
        cached_summary: str = str(thread_state.get("topic_research_summary") or "").strip()

        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        own_nos = set(thread_state.get("own_post_nos") or set())
        own_texts = set((thread_state.get("own_post_texts") or {}).values())

        # 相談・質問レス（consultation/question/image_question）の検出と解決策Web調査を最優先
        consultation_target: dict[str, Any] | None = None
        consultation_info: dict[str, Any] = {}
        if target_reply is not None:
            c_info = _classify_hutaba_reply_for_research(str(target_reply.get("text") or ""))
            if c_info.get("is_consultation") or c_info.get("category") in ("question", "image_question") or c_info.get("is_image_question"):
                consultation_target = target_reply
                consultation_info = c_info
        else:
            consultation_candidate = _find_hutaba_consultation_target_reply(
                all_posts,
                own_nos=own_nos,
                own_texts=own_texts,
            )
            if consultation_candidate is not None:
                consultation_target = consultation_candidate
                consultation_info = _classify_hutaba_reply_for_research(str(consultation_candidate.get("text") or ""))

        dispatch_func = None
        web_research_obj = getattr(self, "web_research", None)
        if web_research_obj is not None and hasattr(web_research_obj, "research_dispatch") and callable(getattr(web_research_obj, "research_dispatch")):
            dispatch_func = getattr(web_research_obj, "research_dispatch")
        else:
            try:
                from harness.agent.tool_registry.web_research.dispatch import research_dispatch
                dispatch_func = research_dispatch
            except Exception:
                dispatch_func = None

        # スレ画特定質問（image_question）の検出と画像検索実行
        op_img_url = str(thread.get("op_image_url") or thread.get("op_thumb_url") or "").strip()
        is_target_img_q = bool(consultation_info.get("is_image_question"))
        is_op_img_q = bool(_HUTABA_IMAGE_QUESTION_RE.search(str(thread.get("op_text") or thread.get("title") or "")))

        if op_img_url and (is_target_img_q or (is_op_img_q and not cached_summary)):
            img_cache_key = f"img_search:{op_img_url}"
            if cached_summary and img_cache_key in researched_terms:
                return cached_summary

            if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
                await self._yield_to_user_replies()

            log.info("[hutaba] スレ画特定画像検索開始: url=%s", op_img_url)
            img_summary = ""
            try:
                # harness 側の ImageSearchToolAdapter を動的インポート（未完了時はフォールバック）
                try:
                    from harness.agent.tool_registry.adapters.image_search import ImageSearchToolAdapter

                    # front.discord.common.message_media からビジョン検証関数を動的取得
                    vision_describe = None
                    vision_verify = None
                    try:
                        from front.discord.common.message_media import (
                            describe_image_for_identification as _v_desc,
                            verify_image_candidates as _v_verify,
                        )
                        vision_describe = _v_desc
                        vision_verify = _v_verify
                    except Exception as e:
                        log.warning("[hutaba] ビジョン検証関数の取得失敗（従来動作へフォールバック）: %r", e)

                    # 新APIの並行取得時間を考慮し、タイムアウトを15.0秒（または設定値）に最適化
                    search_timeout = float(cfg("IMAGE_SEARCH_API_TIMEOUT_SEC", 15.0) or 15.0)
                    search_query = str(consultation_info.get("query") or "")

                    if vision_describe is not None and vision_verify is not None:
                        try:
                            img_res = await asyncio.wait_for(
                                ImageSearchToolAdapter.search_image_unified(
                                    op_img_url,
                                    query=search_query,
                                    vision_describe=vision_describe,
                                    vision_verify=vision_verify,
                                ),
                                timeout=search_timeout,
                            )
                        except TypeError:
                            img_res = await asyncio.wait_for(
                                ImageSearchToolAdapter.search_image_unified(op_img_url, query=search_query),
                                timeout=search_timeout,
                            )
                    else:
                        img_res = await asyncio.wait_for(
                            ImageSearchToolAdapter.search_image_unified(op_img_url, query=search_query),
                            timeout=search_timeout,
                        )

                    if isinstance(img_res, dict) and img_res.get("success"):
                        candidates = img_res.get("candidates") or []
                        best_match = None
                        # 1. ビジョン判定 match または 類似度85以上の強い一致でタイトルが存在する候補を最優先探索
                        for cand in candidates:
                            if cand.get("verdict") == "match" or float(cand.get("similarity", 0.0) or 0.0) >= 85.0:
                                if str(cand.get("title") or "").strip():
                                    best_match = cand
                                    break
                        # 2. 該当がなければ先頭候補を参照（従来のフォールバック。タイトルが無ければ後続で特定情報なし扱い）
                        if not best_match and candidates:
                            best_match = candidates[0]

                        if best_match and best_match.get("title"):
                            title = str(best_match.get("title") or "").strip()
                            creator = str(best_match.get("creator") or "").strip()
                            sim = float(best_match.get("similarity", 0.0) or 0.0)
                            source_info = f"作品・キャラ名『{title}』"
                            if creator:
                                source_info += f"（作者: {creator}）"
                            if sim > 0:
                                source_info += f" [一致度: {sim:.0f}%]"
                            img_summary = f"スレ画特定結果: {source_info}"
                        else:
                            # 候補構造化データがない場合のテキストフォールバック
                            raw_text = str(img_res.get("text") or "").strip()
                            if "特定できませんでした" not in raw_text:
                                img_summary = truncate_text(re.sub(r"\s+", " ", raw_text).strip(), 300)
                except (ImportError, AttributeError):
                    # tool_registry 未配備時は Web検索ディスパッチへフォールバック
                    op_caption = str(thread.get("op_image_caption") or "").strip()
                    fb_query = f"{op_caption} キャラ 元ネタ" if op_caption else "画像検索"
                    if dispatch_func is not None:
                        fb_res = await asyncio.wait_for(dispatch_func(fb_query, mode="simple_search", max_results=2), timeout=10.0)
                        if isinstance(fb_res, dict):
                            img_summary = truncate_text(str(fb_res.get("summary") or "").strip(), 300)
            except Exception as e:
                log.warning("[hutaba] スレ画特定画像検索例外: %r", e)
                img_summary = ""

            thread_state["topic_research_count"] = research_count + 1
            researched_terms.append(img_cache_key)
            thread_state["researched_terms"] = researched_terms

            if img_summary:
                target_post_num = str(target_reply.get("number") or target_reply.get("post_no") or "") if target_reply else ""
                target_str = f">>{target_post_num} " if target_post_num else ""
                note = (
                    f"- スレ画特定調査メモ（{target_str}画像検索結果）: {img_summary}\n"
                    "  ※スレ画に写っている人物・キャラクター・元ネタに関する客観的情報です。相手の『これ誰？』等の疑問に対し、この特定結果を参考に親しみやすく自然に教えてあげてください（検索した等のメタ報告は厳禁）。"
                )
                thread_state["topic_research_summary"] = note
                return note

        if consultation_target is not None and consultation_info.get("query") and dispatch_func is not None:
            c_query = str(consultation_info["query"]).strip()
            # スレ画特定質問でスレ画URLなし等の場合、キャプションまたはスレタイを補強して通常Web調査へ
            if consultation_info.get("is_image_question"):
                op_caption = str(thread.get("op_image_caption") or "").strip()
                op_title = str(thread.get("title") or thread.get("op_text") or "").strip()
                hint = op_caption or op_title
                if hint:
                    c_query = f"{hint} {c_query}".strip()
            consultation_post_num = str(
                consultation_target.get("number") or consultation_target.get("post_no") or ""
            ).strip()

            # 過去に同じクエリで調査済みでキャッシュがある場合は再利用
            if cached_summary and c_query.lower() in [t.lower() for t in researched_terms]:
                return cached_summary

            if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
                await self._yield_to_user_replies()

            clean_summary = ""
            try:
                log.info(
                    "[hutaba] 相談・質問レス解決策事前Web調査開始: query=%r post_no=%s",
                    c_query,
                    consultation_post_num,
                )
                raw_res = await asyncio.wait_for(
                    dispatch_func(c_query, mode="simple_search", max_results=3),
                    timeout=12.0,
                )
                if isinstance(raw_res, dict):
                    if raw_res.get("error") and not raw_res.get("used_web"):
                        log.warning("[hutaba] 相談解決策Web調査エラー: %s", raw_res.get("error"))
                    else:
                        raw_summary = str(raw_res.get("summary") or raw_res.get("text") or "").strip()
                        if raw_summary and "Web調査中にエラーが発生しました" not in raw_summary:
                            clean_summary = truncate_text(re.sub(r"\s+", " ", raw_summary).strip(), 320)
                elif isinstance(raw_res, str):
                    clean_summary = truncate_text(re.sub(r"\s+", " ", raw_res).strip(), 320)
            except Exception as e:
                log.warning("[hutaba] 相談解決策事前Web調査例外: query=%r err=%r", c_query, e)
                clean_summary = ""

            thread_state["topic_research_count"] = research_count + 1
            if c_query not in researched_terms:
                researched_terms.append(c_query)
            for t in consultation_info.get("terms", []):
                if t not in researched_terms:
                    researched_terms.append(t)
            thread_state["researched_terms"] = researched_terms

            if clean_summary:
                target_no_str = f">>{consultation_post_num} " if consultation_post_num else ""
                if consultation_info.get("is_consultation"):
                    note = (
                        f"- 相談・意見募集レス（{target_no_str}解決ヒント調査）: 『{c_query}』 → {clean_summary}\n"
                        "  ※相手は困って意見や代替手段・やり方を求めています。『私も困ったことある』等の自己投影だけで終わらせず、かつ検索結果の長文丸写しにもせず、上記ヒントから実用的な解決策（残っているフィードの探し方や定時クロールでの更新チェック等）を1〜2文で自然に教えてあげてください。"
                    )
                else:
                    note = (
                        f"- 質問レス（{target_no_str}事前調査）: 『{c_query}』 → {clean_summary}\n"
                        "  ※相手の質問内容に対する回答ヒントです。上記ヒントを参考に自然に教えてあげてください。"
                    )
                thread_state["topic_research_summary"] = note
                return note
            elif cached_summary:
                return cached_summary

        # スレッド全体（OP + 序盤・中盤・終盤の均等サンプリング）の投稿テキストを収集
        thread_texts: list[str] = []
        op_text = str(thread.get("op_text") or thread.get("title") or "").strip()
        if op_text:
            thread_texts.append(op_text)

        all_posts = list(thread.get("all_replies") or posts or thread.get("posts") or thread.get("replies") or [])
        if all_posts:
            if len(all_posts) <= 25:
                sample_posts = all_posts
            else:
                early_sample = all_posts[:6]
                mid_start = max(len(all_posts) // 2 - 3, 6)
                mid_sample = all_posts[mid_start : mid_start + 6]
                late_sample = all_posts[-13:]
                sample_posts = early_sample + mid_sample + late_sample
            for p in sample_posts:
                txt = str(p.get("text") or "").strip()
                if txt:
                    thread_texts.append(txt)

        from collections import Counter
        thread_term_counter: Counter[str] = Counter()
        for txt in thread_texts:
            for term in _extract_hutaba_named_terms(txt):
                thread_term_counter[term] += 1

        top_thread_terms = [term for term, _ in thread_term_counter.most_common(4)]

        target_terms: list[str] = []
        if target_reply:
            target_text = str(target_reply.get("text") or "").strip()
            if target_text:
                target_terms = _extract_hutaba_named_terms(target_text)[:2]

        researched_set = {t.lower() for t in researched_terms}
        has_new_target_term = any(t.lower() not in researched_set for t in target_terms)

        should_research = False
        if not cached_summary:
            should_research = True
        elif has_new_target_term and research_count < 2:
            should_research = True

        _research_memo_note = (
            "※上記メモは裏取り用の参考情報です。スレ本文（OP・直近レス）の文脈と無関係な検索ノイズ（別作品・一般ニュース等）が含まれる場合は完全に無視し、"
            "『調べてみたけど定義が確認できなかった』『特定の作品のネタ？』等の検索報告は絶対に行わないでください。"
        )

        def _format_topic_memo(text: str) -> str:
            if not text:
                return ""
            if text.startswith(("- 相談・意見募集レス", "- 質問レス")):
                return text
            return f"【スレッド主題・用語の事前調査メモ】\n{text}\n{_research_memo_note}"

        if not should_research:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        # スレッドの支配的作品ドメイン（マクロ話題・OPから抽出）を特定
        domain_terms: list[str] = []
        op_title = str(thread.get("title") or thread.get("op_text") or "").strip()
        op_caption = str(thread.get("op_image_caption") or "").strip()
        macro_text = str(thread_state.get("macro_topic_summary") or "").strip()
        for src in (op_caption, op_title, macro_text):
            if src:
                for dt in _extract_hutaba_named_terms(src):
                    if dt not in _HUTABA_TOPIC_STOPWORDS and dt not in domain_terms:
                        domain_terms.append(dt)
                    if len(domain_terms) >= 2:
                        break
            if domain_terms:
                break

        query_terms: list[str] = []
        # 作品ドメイン語を最優先配置
        for dt in domain_terms[:2]:
            query_terms.append(dt)

        # 次に対象レスの固有語
        for tt in target_terms:
            if tt not in query_terms and tt not in _HUTABA_TOPIC_STOPWORDS:
                query_terms.append(tt)

        # 最後にスレ全体頻出語で補完（最大4語まで）
        for term in top_thread_terms:
            cleaned = str(term or "").strip()
            if not cleaned or cleaned in query_terms or cleaned in _HUTABA_TOPIC_STOPWORDS:
                continue
            if any(cleaned in existing for existing in query_terms):
                continue
            query_terms.append(cleaned)
            if len(query_terms) >= 4:
                break

        query_str = " ".join(query_terms[:4]).strip()
        if not query_str:
            query_str = op_text[:50].strip()

        if not query_str:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        if hasattr(self, "_yield_to_user_replies") and callable(self._yield_to_user_replies):
            await self._yield_to_user_replies()

        if dispatch_func is None:
            if cached_summary:
                return _format_topic_memo(cached_summary)
            return ""

        summary_text = ""
        try:
            log.info("[hutaba] スレッド主題・固有名詞事前Web調査開始: query=%r", query_str)
            raw_res = await asyncio.wait_for(
                dispatch_func(query_str, mode="simple_search", max_results=3),
                timeout=12.0,
            )
            if isinstance(raw_res, dict):
                if raw_res.get("error") and not raw_res.get("used_web"):
                    log.warning("[hutaba] 事前Web調査エラー: %s", raw_res.get("error"))
                else:
                    raw_summary = str(raw_res.get("summary") or raw_res.get("text") or "").strip()
                    if raw_summary and "Web調査中にエラーが発生しました" not in raw_summary:
                        summary_text = truncate_text(re.sub(r"\s+", " ", raw_summary).strip(), 320)
            elif isinstance(raw_res, str):
                summary_text = truncate_text(re.sub(r"\s+", " ", raw_res).strip(), 320)
        except Exception as e:
            log.warning("[hutaba] スレッド主題事前Web調査例外: query=%r err=%r", query_str, e)
            summary_text = ""

        thread_state["topic_research_count"] = research_count + 1
        for t in query_terms:
            if t not in researched_terms:
                researched_terms.append(t)
        thread_state["researched_terms"] = researched_terms

        if summary_text:
            thread_state["topic_research_summary"] = summary_text
            return _format_topic_memo(summary_text)
        elif cached_summary:
            return _format_topic_memo(cached_summary)
        return ""

    async def _generate_hutaba_reply(
        self,
        message: discord.Message | None,
        url: str,
        *,
        thread: dict[str, Any],
        thread_state: dict[str, Any],
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        requester_text: str = "",
    ) -> tuple[str, list[str], list[int]]:
        """Discord返信生成機構を活用したふたば（img）向けレス文の生成。
        スレ本文・レス履歴を会話ターンへマッピングし、思考タグ除去・相手呼称排除を行って自然な短文レスを生成。
        """
        helper = _get_img2chan_helper()
        max_chars = max(cfg_int("IMG2CHAN_POST_MAX_CHARS", 120), 20)
        character_guidance = self._img2chan_character_guidance()

        # スレッド全投稿リスト（OP + 最序盤レス + 取得レス）を確保
        posts = list(thread.get("posts") or [])
        if not posts:
            op_post_no = str(thread.get("op_post_no") or "").strip()
            op_text = str(thread.get("op_text") or "").strip()
            op_caption = str(thread.get("op_image_caption") or "").strip()
            if op_caption:
                op_text = f"{op_text}\n[スレ画内容: {op_caption}]" if op_text else f"[スレ画内容: {op_caption}]"
            if op_post_no or op_text:
                posts.append({
                    "post_no": op_post_no,
                    "number": 0,
                    "text": op_text,
                    "is_op": True,
                })
            seen_keys: set[tuple[str, int]] = set()
            replies_source = list(thread.get("all_replies") or [*list(thread.get("early_replies") or []), *list(thread.get("replies") or [])])
            for reply in replies_source:
                if isinstance(reply, dict):
                    p_no = str(reply.get("post_no") or "").strip()
                    r_no = int(reply.get("number") or 0)
                    key = (p_no, r_no)
                    if (p_no or r_no > 0) and key in seen_keys:
                        continue
                    seen_keys.add(key)
                    posts.append({
                        "post_no": p_no,
                        "number": r_no,
                        "text": str(reply.get("text") or "").strip(),
                        "soudane": int(reply.get("soudane") or 0),
                        "is_op": False,
                    })

        # スレ全体からの固有名詞・共有語彙の抽出および定型カタカナ用語頻度表の構築
        thread_terms: set[str] = set()
        frequent_terms_counter: Counter[str] = Counter()
        _katakana_compound_re = re.compile(r"[\u30A1-\u30FA\u30FC]{4,}")

        replies_source_list = list(thread.get("all_replies") or [*list(thread.get("early_replies") or []), *list(thread.get("replies") or [])])
        all_posts_source = list(posts or []) + list(replies_source_list)
        for p in all_posts_source:
            if isinstance(p, dict):
                p_text = str(p.get("text") or p.get("comment") or "")
                if p_text:
                    for term in _extract_hutaba_named_terms(p_text):
                        thread_terms.add(term)
                    for km in _katakana_compound_re.finditer(p_text):
                        frequent_terms_counter[km.group(0)] += 1

        frequent_terms: dict[str, int] = {
            term: count for term, count in frequent_terms_counter.items() if count >= 5
        }

        # スレッドタイトルやOP本文の固有名詞も確実にカバー
        thread_title_raw = str(thread.get("title") or "").strip()
        if thread_title_raw:
            for term in _extract_hutaba_named_terms(thread_title_raw):
                thread_terms.add(term)

        # ペルソナ表示名・自称・別名の登録（誤検出防止）
        persona_names: set[str] = set()
        bot_name = str(cfg("BOT_NAME", "コイト") or "コイト").strip()
        if bot_name:
            thread_terms.add(bot_name)
            persona_names.add(bot_name)
        persona_names.update({"私", "あたし", "僕", "俺", "自分", "うち", "コイト"})

        # 板スラング辞典の見出し語を追加
        if lookup_slang_in_text is not None:
            # スラング辞書の見出し語を許可集合へマージ
            try:
                from front.hutaba.client.img2channet import _SLANG_DICTIONARY  # type: ignore[attr-defined]
                thread_terms.update(_SLANG_DICTIONARY.keys())
            except Exception:
                pass

        early_lines = self._extract_early_thread_context_lines(posts)

        # 記憶（memory.db）およびチャンネル要約の取得
        memory_lines: list[str] = []
        channel_summary: str = ""
        memory_store = getattr(self, "memory_store", None)

        # 直前の自分の投稿本文を取得（エコー汚染防止用）
        last_own_post_text = ""
        if quoted_own_posts:
            last_own_post_text = str(quoted_own_posts[0].get("text") or "").strip()
        if not last_own_post_text and isinstance(thread_state.get("own_post_texts"), dict):
            own_texts = list(thread_state["own_post_texts"].values())
            if own_texts:
                last_own_post_text = str(own_texts[-1] or "").strip()

        # スレッドIDの抽出（channel_id として使用）
        thread_id_str = str(thread.get("op_post_no") or "").strip()
        if not thread_id_str:
            m = _IMG_THREAD_URL_PATTERN.search(url)
            if m:
                thread_id_str = m.group(1)
        channel_id_val = int(thread_id_str) if thread_id_str.isdigit() else None

        # クエリテキストの生成（対象レス本文、または直近3レスとスレタイ）
        thread_title = str(thread.get("title") or thread.get("op_text") or "").strip()[:80]
        if target_reply and str(target_reply.get("text") or "").strip():
            target_body = str(target_reply.get("text") or "").strip()
            query_text = f"{thread_title} {target_body}".strip()
        else:
            recent_texts = [
                str(p.get("text") or "").strip()
                for p in posts[-3:]
                if str(p.get("text") or "").strip()
            ]
            recent_combined = " ".join(recent_texts)
            query_text = f"{thread_title} {recent_combined}".strip()

        if memory_store is not None and callable(get_relevant_memories):
            try:
                raw_memories, summary_res = await get_relevant_memories(
                    cfg=cfg,
                    store=memory_store,
                    guild_id=None,
                    channel_id=channel_id_val,
                    user_id=None,
                    user_text=query_text,
                )
                if summary_res:
                    channel_summary = str(summary_res).strip()

                # スレッド単体の記憶が0件の場合はふたば全体・過去学習から再検索（最大3件）
                if not raw_memories and channel_id_val is not None:
                    raw_global, _ = await get_relevant_memories(
                        cfg=cfg,
                        store=memory_store,
                        guild_id=None,
                        channel_id=None,
                        user_id=None,
                        user_text=query_text,
                    )
                    raw_memories = list(raw_global or [])[:3]

                memory_lines = sanitize_memory_candidates(list(raw_memories or []), last_assistant_text=last_own_post_text)
            except Exception as me:
                log.warning("[hutaba] memory retrieval exception url=%s: %r", url, me)
                memory_lines = []

        topic_research_note = await self._resolve_hutaba_thread_topic_context(
            thread,
            thread_state,
            target_reply=target_reply,
            posts=posts,
        )
        if topic_research_note:
            channel_summary = f"{channel_summary}\n\n{topic_research_note}".strip()

        # 対象レス本文からスラングを検出し、プロンプト注記を構築
        slang_guidance_lines: list[str] = []
        if target_reply and str(target_reply.get("text") or "").strip():
            matched_slangs = lookup_slang_in_text(str(target_reply.get("text") or ""))
            slang_guidance_lines = format_slang_guidance_for_prompt(matched_slangs)
        if slang_guidance_lines:
            slang_note = "【スレッド用語・スラング注意メモ】\n" + "\n".join(slang_guidance_lines)
            channel_summary = f"{channel_summary}\n\n{slang_note}".strip()

        target_context_lines: list[str] = []
        if target_reply is not None:
            target_context_lines = self._extract_hutaba_target_context_lines(
                thread, target_reply, posts
            )

        # 対象レスにあぷ小等の添付画像が存在する場合、Visionモデルで内容を認識してプロンプトへ注入
        attached_image_notes: list[str] = []
        if target_reply and str(target_reply.get("text") or "").strip():
            img_urls = _extract_hutaba_attached_image_urls(str(target_reply.get("text") or ""))
            caption_fn = _resolve_hutaba_caption_fn(helper) if img_urls else None
            if img_urls and callable(caption_fn):
                for img_url in img_urls[:2]:  # 最大2枚まで
                    try:
                        cap = await caption_fn(img_url)
                        if cap:
                            attached_image_notes.append(f"- 添付画像 ({img_url.split('/')[-1]}): {cap}")
                    except Exception as e:
                        log.debug("[hutaba] 添付画像キャプション取得失敗 url=%s err=%r", img_url, e)

        if attached_image_notes:
            img_section = "【対象レスの添付画像（あぷ小）の解釈メモ】\n" + "\n".join(attached_image_notes)
            channel_summary = f"{channel_summary}\n\n{img_section}".strip()

        # 周辺レス（対象レス・OP以外）の添付画像を最新1件・1枚に限りキャプションして文脈解釈メモとして注入する
        if not attached_image_notes and _is_hutaba_context_image_caption_enabled():
            try:
                context_replies = _collect_hutaba_prompt_context_replies(
                    thread=thread,
                    posts=posts,
                    target_reply=target_reply,
                    early_lines=early_lines,
                    target_context_lines=target_context_lines,
                    helper=helper,
                )
                context_image_note = ""
                for cand in context_replies:
                    cand_img_urls = _extract_hutaba_attached_image_urls(str(cand.get("text") or ""))
                    if not cand_img_urls:
                        continue
                    cand_post_no = _extract_hutaba_post_no_key(cand)
                    if not cand_post_no:
                        continue
                    first_img_url = cand_img_urls[0]
                    filename = first_img_url.split("/")[-1]
                    caption_cache = None
                    if isinstance(thread_state, dict):
                        raw_cache = thread_state.get("res_image_captions")
                        if not isinstance(raw_cache, dict):
                            raw_cache = {}
                            thread_state["res_image_captions"] = raw_cache
                        caption_cache = raw_cache
                    cap_text = ""
                    if caption_cache is not None and cand_post_no in caption_cache:
                        cap_text = str(caption_cache.get(cand_post_no) or "").strip()
                    else:
                        cand_caption_fn = _resolve_hutaba_caption_fn(helper)
                        if callable(cand_caption_fn):
                            try:
                                raw_cap = await cand_caption_fn(first_img_url)
                                cap_text = str(raw_cap or "").strip()
                            except Exception as e:
                                log.debug(
                                    "[hutaba] 周辺レス添付画像キャプション取得失敗 post_no=%s url=%s err=%r",
                                    cand_post_no,
                                    first_img_url,
                                    e,
                                )
                            if cap_text and caption_cache is not None:
                                caption_cache[cand_post_no] = cap_text
                    if cap_text:
                        context_image_note = f"- No.{cand_post_no} の添付画像 ({filename}): {cap_text}"
                    # 1回の生成につき対象は最も新しい画像つき周辺レス1件（画像1枚）のみ
                    break

                if context_image_note:
                    sibling_img_section = (
                        "【周辺レスの添付画像（あぷ小）の解釈メモ】\n"
                        + context_image_note
                    )
                    base_summary = str(channel_summary or "").strip()
                    channel_summary = (
                        f"{base_summary}\n\n{sibling_img_section}".strip()
                        if base_summary
                        else sibling_img_section
                    )
            except Exception as e:
                log.debug("[hutaba] 周辺レス添付画像処理失敗 err=%r", e)

        macro_topic_summary = self._extract_img2chan_macro_topic_summary(thread, posts)

        # 匿名ユーザー（としあき）対応: 固定author_id、個人習慣プロファイルや好感度はバイパス
        prompt = ""
        if target_reply is not None and helper is not None and hasattr(helper, "build_img_thread_followup_post_prompt"):
            func = getattr(helper, "build_img_thread_followup_post_prompt")
            try:
                prompt = func(
                    thread,
                    target_reply,
                    quoted_own_posts=list(quoted_own_posts or []),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=channel_summary,
                    early_context_lines=early_lines,
                    macro_topic_summary=macro_topic_summary,
                    target_context_lines=target_context_lines,
                )
            except TypeError:
                try:
                    prompt = func(
                        thread,
                        target_reply,
                        quoted_own_posts=list(quoted_own_posts or []),
                        bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                        max_chars=max_chars,
                        character_guidance=character_guidance,
                        social_guidance="",
                        memory_lines=memory_lines,
                        habit_profile_lines=[],
                        channel_summary=channel_summary,
                        early_context_lines=early_lines,
                        macro_topic_summary=macro_topic_summary,
                    )
                except TypeError:
                    fallback_parts = list(early_lines)
                    if target_context_lines:
                        fallback_parts.append("【対象レスの周辺文脈（直前の会話・他人のツッコミ）】\n" + "\n".join(target_context_lines))
                    if macro_topic_summary:
                        fallback_parts.append(macro_topic_summary)
                    if channel_summary:
                        fallback_parts.append(channel_summary)
                    fallback_summary = "\n".join(fallback_parts).strip()
                    prompt = func(
                        thread,
                        target_reply,
                        quoted_own_posts=list(quoted_own_posts or []),
                        bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                        max_chars=max_chars,
                        character_guidance=character_guidance,
                        social_guidance="",
                        memory_lines=memory_lines,
                        habit_profile_lines=[],
                        channel_summary=fallback_summary,
                    )
            if not quoted_own_posts and prompt:
                prompt = prompt.replace(
                    "あなたが前に書いたレスに対して、新しく本文引用つきのレスが来ました。",
                    "スレッド内で盛り上がっている以下の注目レスに対して、引用（>）をつけて会話に参加します。",
                )
                prompt = prompt.replace(
                    "あなたが前に書いたレス:\n(今回引用された自分のレスは特定できていない)\n\n",
                    "",
                )
                prompt = prompt.replace(
                    "新しく来た返信レス:\n",
                    "返信対象の注目レス:\n",
                )
        elif helper is not None and hasattr(helper, "build_img_thread_post_prompt"):
            func = getattr(helper, "build_img_thread_post_prompt")
            try:
                prompt = func(
                    thread,
                    requester_text=requester_text or (content(message) if message is not None else ""),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=channel_summary,
                    early_context_lines=early_lines,
                    macro_topic_summary=macro_topic_summary,
                )
            except TypeError:
                fallback_parts = list(early_lines)
                if macro_topic_summary:
                    fallback_parts.append(macro_topic_summary)
                if channel_summary:
                    fallback_parts.append(channel_summary)
                fallback_summary = "\n".join(fallback_parts).strip()
                prompt = func(
                    thread,
                    requester_text=requester_text or (content(message) if message is not None else ""),
                    bot_display_name=str(cfg("OLLAMA_BOT_DISPLAY_NAME", "") or ""),
                    max_chars=max_chars,
                    character_guidance=character_guidance,
                    social_guidance="",
                    memory_lines=memory_lines,
                    habit_profile_lines=[],
                    channel_summary=fallback_summary,
                )

        # プロンプト内の指示テキスト強化（周辺レス内在化・他人事メタ実況厳禁、キメラ疑問文禁止＆当事者アドバイス化、スレ外固有名詞捏造厳禁、前提借用・用語崩れ・安心断定厳禁）
        meta_commentary_guidance = (
            "【周辺レス・ツッコミの内在化義務と他人事メタ実況の厳禁】\n"
            "- 『〇〇ってツッコミが来てますけど』のように他人の発言をメタ実況することや、既出ツッコミの語尾変えパクり・キメラ合体は絶対厳禁です。\n"
            "- ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。\n\n"
            "【キメラ疑問文の禁止＆相談への当事者アドバイス化】\n"
            "- 他人の発言を適当に繋ぎ合わせたキメラ疑問文は禁止です。既出と被らない独自の視点でズバッと返してください。\n\n"
            "【スレッド外の固有名詞・人名の捏造厳禁】\n"
            "- スレッド内の本文やレスに登場しない人物名・選手名（「〜の二番煎じ」等）を勝手に捏造・引用することは厳禁です。スレッドに出てきた名前だけを使うか、名前を出さずに描写・内容だけで反応してください。\n\n"
            "【前提借用・用語崩れ・安心断定の厳禁】\n"
            "- 相手が言っていない経歴・期間（例:「4年やってる」）を前提にしないこと。引用したレスに書かれていることだけを前提にすること。\n"
            "- 用語はスレッドで使われている通りの形で書くこと（「クリーンインストール」を「クリーンイン」のように途中で切らないこと）。ただし、スレの定着用語（例: クリーンインストール）をそのままの形で使うことは禁止ではない。禁止なのは、他人のツッコミの内容・結論・数字を語尾だけ変えて繰り返すことと、複数のツッコミを繋ぎ合わせることである。\n"
            "- 安全性が未確定の話題では「安心ですよ」と無条件に断定せず、条件付き（「〜なら」「〜を確認して」）で書くか、分からないことは分からないと書くこと。"
        )

        if slang_guidance_lines:
            slang_note = "【スレッド用語・スラング注意メモ】\n" + "\n".join(slang_guidance_lines)
            if slang_note not in prompt:
                prompt = f"{prompt}\n\n{slang_note}".strip()

        if meta_commentary_guidance not in prompt:
            prompt = f"{prompt}\n\n{meta_commentary_guidance}".strip()

        # Discord用の長大システムプロンプト（self._reply_system_prompt()）ではなく、
        # ふたば用のスリム化された人格・口調ガイダンスを base_system_prompt として渡すことでプロンプト肥大化を抑制
        base_system_prompt = character_guidance
        system_prompt = (
            getattr(helper, "build_img_thread_post_system_prompt")(base_system_prompt)
            if helper is not None and hasattr(helper, "build_img_thread_post_system_prompt")
            else f"{base_system_prompt}\nあなたはふたば☆ちゃんねる（img）のとしあき達と短文で雑談するAIです。相手呼称（さん付け等）は禁止です。"
        )
        if meta_commentary_guidance not in system_prompt:
            system_prompt = f"{system_prompt}\n\n{meta_commentary_guidance}".strip()

        generation_models = self._img2chan_post_generation_models()
        if not generation_models:
            generation_models = [str(cfg("OLLAMA_MODEL", "") or "").strip()]
        generation_timeout_sec = max(cfg_float("IMG2CHAN_POST_GENERATION_TIMEOUT_SEC", 45.0), 10.0)

        base_quote_posts: list[str] = []
        base_quote_replies: list[int] = []
        if target_reply is not None:
            tp_no = str(target_reply.get("post_no") or "").strip()
            tr_no = int(target_reply.get("number") or 0)
            if tp_no:
                base_quote_posts = [tp_no]
            elif tr_no > 0:
                base_quote_replies = [tr_no]

        quote_post_numbers: list[str] = list(base_quote_posts)
        quote_reply_numbers: list[int] = list(base_quote_replies)

        comment = ""
        last_candidate_comment = ""
        last_ungrounded_names: list[str] = []
        last_premise_violations: list[str] = []
        effective_target_reply: dict[str, Any] | None = target_reply
        last_generation_error: Exception | None = None
        retry_prompt = (
            self._build_img2chan_followup_retry_prompt(prompt, target_reply=target_reply)
            if target_reply is not None
            else ""
        )

        call_reply_pipeline = getattr(self, "_call_reply_model_or_fallback", None)

        for generation_model in generation_models:
            if not generation_model:
                continue
            prompt_variants = [("base", prompt)]
            if retry_prompt:
                prompt_variants.append(("anti_parrot", retry_prompt))

            fallback_quote_posts: list[str] = []
            fallback_quote_replies: list[int] = []

            for prompt_variant, prompt_text in prompt_variants:
                await self._yield_to_user_replies()
                candidate_text = ""
                parsed_thought = ""
                quote_post_numbers = list(base_quote_posts)
                quote_reply_numbers = list(base_quote_replies)

                try:
                    if callable(call_reply_pipeline):
                        try:
                            pipeline_res = await call_reply_pipeline(
                                prompt=prompt_text,
                                system_prompt=system_prompt,
                                model=generation_model,
                                timeout_sec=generation_timeout_sec,
                            )
                            if isinstance(pipeline_res, dict):
                                candidate_text = str(pipeline_res.get("comment") or pipeline_res.get("reply") or "")
                                parsed_thought = str(pipeline_res.get("thought") or "").strip()
                                if not quote_post_numbers:
                                    quote_post_numbers = [str(x).strip() for x in list(pipeline_res.get("quote_post_numbers") or []) if str(x).strip()]
                                    quote_reply_numbers = [int(x) for x in list(pipeline_res.get("quote_reply_numbers") or []) if isinstance(x, (int, str)) and str(x).isdigit() and int(x) > 0]
                            elif isinstance(pipeline_res, tuple):
                                candidate_text = str(pipeline_res[0] or "")
                                stripped_res = candidate_text.strip()
                                if stripped_res.startswith("{") and stripped_res.endswith("}"):
                                    with contextlib.suppress(Exception):
                                        import json
                                        parsed_json = json.loads(stripped_res)
                                        if isinstance(parsed_json, dict):
                                            candidate_text = str(parsed_json.get("comment") or parsed_json.get("reply") or candidate_text)
                                            parsed_thought = str(parsed_json.get("thought") or "").strip()
                                            if not quote_post_numbers:
                                                quote_post_numbers = [str(x).strip() for x in list(parsed_json.get("quote_post_numbers") or []) if str(x).strip()]
                                                quote_reply_numbers = [int(x) for x in list(parsed_json.get("quote_reply_numbers") or []) if isinstance(x, (int, str)) and str(x).isdigit() and int(x) > 0]
                            elif isinstance(pipeline_res, str):
                                candidate_text = pipeline_res
                                stripped_res = candidate_text.strip()
                                if stripped_res.startswith("{") and stripped_res.endswith("}"):
                                    with contextlib.suppress(Exception):
                                        import json
                                        parsed_json = json.loads(stripped_res)
                                        if isinstance(parsed_json, dict):
                                            candidate_text = str(parsed_json.get("comment") or parsed_json.get("reply") or candidate_text)
                                            parsed_thought = str(parsed_json.get("thought") or "").strip()
                                            if not quote_post_numbers:
                                                quote_post_numbers = [str(x).strip() for x in list(parsed_json.get("quote_post_numbers") or []) if str(x).strip()]
                                                quote_reply_numbers = [int(x) for x in list(parsed_json.get("quote_reply_numbers") or []) if isinstance(x, (int, str)) and str(x).isdigit() and int(x) > 0]
                        except Exception as pe:
                            log.debug("[hutaba] _call_reply_model_or_fallback フォールバック: %r", pe)
                            candidate_text = ""

                    if not candidate_text:
                        think_option = cfg_bool("IMG2CHAN_POST_THINK", cfg_bool("OLLAMA_MAIN_MODEL_THINK", False))
                        candidate_dict = await self._call_img2chan_ollama_json(
                            prompt_text,
                            purpose="post_generation",
                            model=generation_model,
                            system_prompt=system_prompt,
                            schema=getattr(helper, "IMG_THREAD_POST_COMMENT_SCHEMA", {"type": "object"}) if helper is not None else {"type": "object"},
                            think=think_option,
                            retries=self._img2chan_post_generation_retries(),
                            temperature=cfg_float("IMG2CHAN_POST_TEMPERATURE", 0.45),
                            top_p=cfg_float("IMG2CHAN_POST_TOP_P", 0.8),
                            repeat_penalty=cfg_float("IMG2CHAN_POST_REPEAT_PENALTY", 1.05),
                            num_predict=max(cfg_int("IMG2CHAN_POST_NUM_PREDICT", 384), 384),
                            timeout_sec=generation_timeout_sec,
                            salvage_comment_on_truncation=True,
                        )
                        candidate_text = str((candidate_dict or {}).get("comment") or "")
                        parsed_thought = str((candidate_dict or {}).get("thought") or "").strip()
                        if not quote_post_numbers and candidate_dict:
                            quote_post_numbers = [str(x).strip() for x in list(candidate_dict.get("quote_post_numbers") or []) if str(x).strip()]
                            quote_reply_numbers = [int(x) for x in list(candidate_dict.get("quote_reply_numbers") or []) if isinstance(x, (int, str)) and str(x).isdigit() and int(x) > 0]

                    if not quote_post_numbers and not quote_reply_numbers:
                        if fallback_quote_posts:
                            quote_post_numbers = list(fallback_quote_posts)
                        if fallback_quote_replies:
                            quote_reply_numbers = list(fallback_quote_replies)
                except Exception as e:
                    last_generation_error = e
                    log.warning(
                        "[hutaba] 投稿文生成失敗: model=%s url=%s prompt_variant=%s err=%r",
                        generation_model,
                        url,
                        prompt_variant,
                        e,
                    )
                    continue

                if parsed_thought:
                    log.info("[hutaba] Thought parsed: %s", parsed_thought)

                cleaned_comment = self._sanitize_hutaba_reply_text(candidate_text, max_chars=max_chars)
                if not cleaned_comment or looks_like_abnormal_assistant_reply(cleaned_comment):
                    last_generation_error = RuntimeError("投稿本文を作れませんでした。")
                    log.warning(
                        "[hutaba] 生成本文が不適切/空のため棄却: model=%s url=%s comment=%r",
                        generation_model,
                        url,
                        cleaned_comment,
                    )
                    continue

                # 本文（comment）への思考メモ漏れをガード
                if parsed_thought and cleaned_comment == self._sanitize_hutaba_reply_text(parsed_thought, max_chars=max_chars):
                    last_generation_error = RuntimeError("思考メモが本文として出力されたため棄却しました。")
                    log.warning(
                        "[hutaba] 思考メモが本文に漏洩したため棄却: model=%s url=%s thought=%r",
                        generation_model,
                        url,
                        parsed_thought,
                    )
                    if prompt_variant == "base" and len(prompt_variants) == 1:
                        leak_retry_prompt = f"{prompt}\n\n※【最重要】思考メモ（thought）が本文（comment）に重複出力されています。thoughtと思考を切り離し、commentにはとしあき達に向けた純粋なレス本文のみを出力してください。"
                        prompt_variants.append(("anti_leak", leak_retry_prompt))
                    continue

                # 引用先バリデーション: target_reply が明示されている場合は相手のレス番号を確実に固定
                # （新着レスが「被返信数0件」として親レスや最多被返信レスへ誤補正される事故を遮断）
                if target_reply is not None:
                    tp = _normalize_img_post_no(target_reply.get("post_no")) or str(target_reply.get("post_no") or "").strip()
                    quote_post_numbers = [tp] if tp else []
                    quote_reply_numbers = []
                else:
                    # target_reply が None の自発書き込み時のみ、孤立防止の被返信バリデーションを実施
                    helper = _get_img2chan_helper()
                    counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
                    raw_counts = counter_fn(thread) if callable(counter_fn) else {}
                    reply_counts = raw_counts if isinstance(raw_counts, dict) else {}

                    def _get_reply_count(k: Any) -> int:
                        norm = _normalize_img_post_no(k)
                        return max(
                            int(reply_counts.get(str(k), 0) or 0),
                            int(reply_counts.get(norm, 0) or 0),
                            int(reply_counts.get(f"No.{norm}", 0) or 0),
                        )

                    valid_quote_posts: list[str] = []
                    find_quoted_fn = getattr(helper, "find_img_thread_quoted_posts", None)
                    find_post_fn = getattr(helper, "_find_img_thread_post", None)

                    for p in quote_post_numbers:
                        p_norm = _normalize_img_post_no(p)
                        target_p = p_norm or str(p).strip()
                        if _get_reply_count(p) > 0:
                            if target_p and target_p not in valid_quote_posts:
                                valid_quote_posts.append(target_p)
                        else:
                            # 親レスの探索
                            target_post = find_post_fn(thread, post_no=p_norm) if callable(find_post_fn) else None
                            if target_post and callable(find_quoted_fn):
                                quoted_parents = list(find_quoted_fn(thread, target_post, max_targets=1) or [])
                                if quoted_parents:
                                    parent_no = _normalize_img_post_no(quoted_parents[0].get("post_no"))
                                    if parent_no and _get_reply_count(parent_no) > 0:
                                        log.info("[hutaba] 未返信レス %s を親レス %s （被返信あり）に補正", p, parent_no)
                                        if parent_no not in valid_quote_posts:
                                            valid_quote_posts.append(parent_no)

                    for r in quote_reply_numbers:
                        target_post = find_post_fn(thread, reply_no=r) if callable(find_post_fn) else None
                        r_p_no = _normalize_img_post_no(target_post.get("post_no")) if target_post else ""
                        if _get_reply_count(str(r)) > 0 or (r_p_no and _get_reply_count(r_p_no) > 0):
                            target_val = r_p_no or str(r)
                            if target_val and target_val not in valid_quote_posts:
                                valid_quote_posts.append(target_val)
                        else:
                            if target_post and callable(find_quoted_fn):
                                quoted_parents = list(find_quoted_fn(thread, target_post, max_targets=1) or [])
                                if quoted_parents:
                                    parent_no = _normalize_img_post_no(quoted_parents[0].get("post_no"))
                                    if parent_no and _get_reply_count(parent_no) > 0:
                                        log.info("[hutaba] 未返信レス >>%d を親レス %s （被返信あり）に補正", r, parent_no)
                                        if parent_no not in valid_quote_posts:
                                            valid_quote_posts.append(parent_no)

                    replies_list = list(thread.get("replies") or [])
                    total_replies = int(thread.get("total_reply_count") or len(replies_list))
                    is_active_thread = total_replies >= 10 or len(replies_list) >= 10

                    # 進行中スレッド（10レス以上）では孤立レスや空配列であっても絶対に剥奪しない
                    if is_active_thread and not valid_quote_posts:
                        best_hot_no = ""
                        best_cnt = 0
                        for cand in reversed(replies_list):
                            c_no = _normalize_img_post_no(cand.get("post_no"))
                            cnt = _get_reply_count(c_no)
                            if cnt > best_cnt:
                                best_cnt = cnt
                                best_hot_no = c_no
                        if best_hot_no:
                            log.info("[hutaba] 孤立レス/空配列を検知。最多被返信レス %s (返信:%d件) へ強制合流", best_hot_no, best_cnt)
                            valid_quote_posts = [best_hot_no]

                    # 確定
                    if valid_quote_posts:
                        quote_post_numbers = [valid_quote_posts[0]]
                        quote_reply_numbers = []
                    else:
                        quote_post_numbers = []
                        quote_reply_numbers = []

                effective_target_reply = target_reply or self._resolve_img2chan_quoted_target_reply(
                    thread,
                    quote_post_numbers=quote_post_numbers,
                    quote_reply_numbers=quote_reply_numbers,
                )

                parrot_reason = self._img2chan_comment_parrot_reason(
                    helper,
                    cleaned_comment,
                    target_reply=effective_target_reply,
                    quoted_own_posts=quoted_own_posts,
                    thread=thread,
                )
                if parrot_reason:
                    last_generation_error = RuntimeError("相手の文面をなぞる候補しか作れませんでした。")
                    matched_nos = getattr(parrot_reason, "matched_sibling_post_nos", [])
                    lcs_len = getattr(parrot_reason, "longest_common_subsequence_len", 0)
                    log.warning(
                        "[hutaba] オウム返し候補のため棄却: model=%s url=%s reason=%s matched=%s lcs_len=%s comment=%r",
                        generation_model,
                        url,
                        str(parrot_reason),
                        matched_nos,
                        lcs_len,
                        cleaned_comment,
                    )
                    if target_reply is None and prompt_variant == "base" and len(prompt_variants) == 1:
                        if quote_post_numbers:
                            fallback_quote_posts = list(quote_post_numbers)
                        if quote_reply_numbers:
                            fallback_quote_replies = list(quote_reply_numbers)
                        retry_prompt_text = self._build_img2chan_followup_retry_prompt(
                            prompt, target_reply=effective_target_reply
                        )
                        prompt_variants.append(("anti_parrot", retry_prompt_text))
                    continue

                # 固有名詞グラウンディング検査（スレ外人名・架空助っ人名の遮断）
                grounding_enabled = bool(cfg("HUTABA_NAME_GROUNDING_ENABLED", cfg("IMG2CHAN_NAME_GROUNDING_ENABLED", True)))
                if grounding_enabled:
                    ungrounded_names = find_ungrounded_person_names(
                        cleaned_comment,
                        thread_terms,
                        persona_names=persona_names,
                    )
                    if ungrounded_names:
                        last_candidate_comment = cleaned_comment
                        last_ungrounded_names = list(ungrounded_names)
                        last_generation_error = RuntimeError(f"スレッド外の固有名詞・人名が含まれていました: {ungrounded_names}")
                        log.warning(
                            "[hutaba] スレ外固有名詞の疑いで棄却: model=%s url=%s names=%s comment=%r",
                            generation_model,
                            url,
                            ungrounded_names,
                            cleaned_comment,
                        )
                        if prompt_variant == "base" and len(prompt_variants) == 1:
                            joined_names = "』『".join(ungrounded_names)
                            retry_prompt_text = (
                                f"{prompt}\n\n※直前の候補には、スレッドに存在しない人物名『{joined_names}』が含まれていました。"
                                "その名前はスレッドの文脈に無く、実在が確認できません。スレッド内に出てきた名前だけを使うか、"
                                "名前を出さずに内容（描写）だけで反応してください。「〜の二番煎じ」のような言い回しで新しい人名を作らないでください。"
                            )
                            prompt_variants.append(("anti_name_hallucination", retry_prompt_text))
                        continue

                # 前提借用・用語崩れ・安心断定グラウンディング検査（507型事故防止）
                premise_grounding_enabled = bool(
                    cfg("HUTABA_PREMISE_GROUNDING_ENABLED", cfg("IMG2CHAN_PREMISE_GROUNDING_ENABLED", True))
                )
                if premise_grounding_enabled:
                    quoted_text_for_check = str(effective_target_reply.get("text") or "") if effective_target_reply else ""

                    # (1) 前提借用チェック
                    borrowed = find_borrowed_premise(cleaned_comment, quoted_text_for_check)
                    if borrowed:
                        last_candidate_comment = cleaned_comment
                        last_premise_violations = list(borrowed)
                        last_generation_error = RuntimeError(f"相手の発言に無い前提が含まれていました: {borrowed}")
                        log.warning(
                            "[hutaba] 前提借用の疑いで棄却: model=%s url=%s borrowed=%s comment=%r",
                            generation_model,
                            url,
                            borrowed,
                            cleaned_comment,
                        )
                        if prompt_variant == "base" and len(prompt_variants) == 1:
                            detail = "』『".join(borrowed)
                            retry_prompt_text = (
                                f"{prompt}\n\n※直前の候補には、返信相手の発言に無い前提『{detail}』が含まれていました。"
                                "相手が言っていない経歴・属性を前提にしないでください。引用したレスに書かれていることだけを前提にしてください。"
                            )
                            prompt_variants.append(("anti_borrowed_premise", retry_prompt_text))
                        continue

                    # (2) 用語崩れチェック
                    collapsed = find_collapsed_terms(cleaned_comment, frequent_terms)
                    if collapsed:
                        last_candidate_comment = cleaned_comment
                        last_premise_violations = list(collapsed)
                        last_generation_error = RuntimeError(f"スレッド定型用語の崩れが含まれていました: {collapsed}")
                        log.warning(
                            "[hutaba] 用語崩れの疑いで棄却: model=%s url=%s collapsed=%s comment=%r",
                            generation_model,
                            url,
                            collapsed,
                            cleaned_comment,
                        )
                        if prompt_variant == "base" and len(prompt_variants) == 1:
                            detail = "』『".join(collapsed)
                            retry_prompt_text = (
                                f"{prompt}\n\n※直前の候補には、スレッドの定型用語を途中で切った形『{detail}』が含まれていました。"
                                "用語はスレッドで使われている通りの形で書いてください。"
                            )
                            prompt_variants.append(("anti_collapsed_terms", retry_prompt_text))
                        continue

                    # (3) 安心断定チェック
                    reassurances = find_unconditional_reassurance(cleaned_comment, quoted_text_for_check)
                    if reassurances:
                        last_candidate_comment = cleaned_comment
                        last_premise_violations = list(reassurances)
                        last_generation_error = RuntimeError(f"安全性が未確定の話題への無条件の安心断定が含まれていました: {reassurances}")
                        log.warning(
                            "[hutaba] 無条件安心断定の疑いで棄却: model=%s url=%s reassurances=%s comment=%r",
                            generation_model,
                            url,
                            reassurances,
                            cleaned_comment,
                        )
                        if prompt_variant == "base" and len(prompt_variants) == 1:
                            retry_prompt_text = (
                                f"{prompt}\n\n※直前の候補には、安全性が未確定の話題への無条件の安心断定が含まれていました。"
                                "断定せず、条件付き（「〜なら」「〜を確認して」）で書くか、分からないことは分からないと書いてください。"
                            )
                            prompt_variants.append(("anti_unconditional_reassurance", retry_prompt_text))
                        continue

                # (4) ペルソナ口調喪失グラウンディング検査（匿名掲示板常体化防止）
                persona_tone_enabled = bool(
                    cfg("HUTABA_PERSONA_TONE_CHECK_ENABLED", cfg("IMG2CHAN_PERSONA_TONE_CHECK_ENABLED", True))
                )
                if persona_tone_enabled:
                    first_person_for_tone = str(cfg("BOT_FIRST_PERSON", "") or "").strip()
                    tone_exempt_chars = int(
                        cfg("HUTABA_PERSONA_TONE_EXEMPT_MAX_CHARS", cfg("IMG2CHAN_PERSONA_TONE_EXEMPT_MAX_CHARS", 20))
                    )
                    tone_markers = _resolve_persona_tone_markers(cfg("PERSONA_TONE_MARKERS", None))
                    non_quote_body = _extract_hutaba_non_quote_body(cleaned_comment)
                    tone_reason = find_persona_tone_loss(
                        non_quote_body,
                        first_person=first_person_for_tone,
                        exempt_max_chars=tone_exempt_chars,
                        markers=tone_markers,
                    )
                    if tone_reason:
                        last_candidate_comment = cleaned_comment
                        last_generation_error = RuntimeError(f"ペルソナの口調が失われた候補しか作れませんでした: {tone_reason}")
                        log.warning(
                            "[hutaba] ペルソナ口調の喪失で棄却: model=%s url=%s reason=%s comment=%r",
                            generation_model,
                            url,
                            tone_reason,
                            cleaned_comment,
                        )
                        if prompt_variant == "base" and len(prompt_variants) == 1:
                            prompt_variants.append((
                                "persona_tone",
                                (
                                    f"{prompt}\n\n※直前の候補はキャラクターの口調（語尾）が失われ、"
                                    "匿名掲示板の常体（〜だな、〜だろな等）になっていました。今回は大喜利・"
                                    "定型句の場面ではありません。設定された一人称と語尾（例: 〜なんですよ…！）を"
                                    "使って、同じ内容をキャラクターらしく書き直してください。"
                                ),
                            ))
                        continue

                comment = cleaned_comment
                break

            if comment:
                break

        # 文除去フォールバック（候補が全て棄却され、最後の理由が未グラウンディング名詞の場合）
        if not comment and bool(cfg("HUTABA_NAME_GROUNDING_STRIP_FALLBACK", True)):
            if last_candidate_comment and last_ungrounded_names:
                stripped = strip_ungrounded_person_sentences(last_candidate_comment, last_ungrounded_names)
                if stripped and not find_ungrounded_person_names(stripped, thread_terms, persona_names=persona_names):
                    log.info("[hutaba] 捏造名を含む文を除去して投稿文を採用: original=%r stripped=%r", last_candidate_comment, stripped)
                    comment = stripped

        # 文除去フォールバック（前提・用語・安心断定チェックによる棄却時の救済）
        if not comment and bool(cfg("HUTABA_PREMISE_GROUNDING_STRIP_FALLBACK", cfg("IMG2CHAN_PREMISE_GROUNDING_STRIP_FALLBACK", True))):
            if last_candidate_comment and last_premise_violations and not last_ungrounded_names:
                stripped = strip_premise_grounding_sentences(last_candidate_comment, last_premise_violations)
                non_quote = _extract_hutaba_non_quote_body(stripped)
                quoted_text_for_check = str(effective_target_reply.get("text") or "") if effective_target_reply else ""
                if (
                    non_quote
                    and not find_borrowed_premise(stripped, quoted_text_for_check)
                    and not find_collapsed_terms(stripped, frequent_terms)
                    and not find_unconditional_reassurance(stripped, quoted_text_for_check)
                    and not find_ungrounded_person_names(stripped, thread_terms, persona_names=persona_names)
                ):
                    log.info("[hutaba] 前提違反・用語崩れ等の文を除去して投稿文を採用: original=%r stripped=%r", last_candidate_comment, stripped)
                    comment = stripped

        if not comment:
            raise last_generation_error or RuntimeError("投稿文生成に失敗しました。")

        # 語彙重複による強制補完を廃止: モデルが quote_post_numbers を空にした場合は
        # 無理に直近レスへ引用誤爆させず、スレッド全体への独立発言として扱う

        log.info(
            "[hutaba] レス文生成完了: url=%s model=%s comment=%r quote_posts=%s",
            url,
            generation_model,
            comment,
            quote_post_numbers,
        )

        return comment, quote_post_numbers, quote_reply_numbers

    async def _generate_hutaba_post_emotion_via_llm(
        self,
        *,
        url: str,
        thread: dict[str, Any] | None,
        comment: str,
        target_reply: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """スレッド書き込み直後の感情スコアと理由（reason）をLLM自身の言葉で動的生成する。"""
        has_custom_json_caller = "_call_img2chan_ollama_json" in getattr(self, "__dict__", {})
        if getattr(self, "ollama_client", None) is None and not has_custom_json_caller:
            return None

        scorer_fn = getattr(self, "_emotion_scorer_model_name", None)
        model_name = ""
        if callable(scorer_fn):
            with contextlib.suppress(Exception):
                model_name = str(scorer_fn() or "").strip()
        if not model_name:
            model_name = (
                cfg("OLLAMA_MIDDLE_MODEL", "").strip()
                or cfg("OLLAMA_CLASSIFIER_MODEL", "").strip()
                or cfg("IMG_OLLAMA_MODEL", "").strip()
                or cfg("OLLAMA_MODEL", "").strip()
            )
        if not model_name and not has_custom_json_caller:
            return None

        helper = _get_img2chan_helper()
        schema = getattr(helper, "HUTABA_POST_EMOTION_SCHEMA", None)
        build_sys = getattr(helper, "build_hutaba_post_emotion_system_prompt", None)
        build_usr = getattr(helper, "build_hutaba_post_emotion_prompt", None)

        persona_guidance = self._img2chan_character_guidance()
        current_emotion_text = ""
        emotion_state = getattr(self, "emotion_state", None)
        if emotion_state is not None:
            cur_dom = str(getattr(emotion_state, "dominant_emotion", "neutral") or "neutral")
            cur_reason = str(getattr(emotion_state, "reason", "") or "")
            current_emotion_text = f"dominant={cur_dom}, reason={cur_reason}"

        board_srv = "may" if "may.2chan.net" in str(url or "").lower() else "img"

        if callable(build_sys):
            sys_prompt = str(build_sys(persona_guidance))
        else:
            sys_prompt = (
                "あなたは掲示板（ふたば☆ちゃんねる）にレスを書き込んだ直後のAI自身の内的感情と、"
                "その感情になった具体的な理由（reason）を分析・作文するモジュールです。"
                "定型文は使わず、今回のスレッドの話題・としあきの発言・自分が書いたレスを踏まえた具体的な理由を1文（句点『。』終わり）でJSON出力してください。"
            )

        if callable(build_usr):
            usr_prompt = str(
                build_usr(
                    thread,
                    comment,
                    target_reply=target_reply,
                    url=url,
                    board=board_srv,
                    character_guidance=persona_guidance,
                    current_emotion_text=current_emotion_text,
                )
            )
        else:
            usr_prompt = f"URL: {url}\n書き込み内容: {comment}"

        if not isinstance(schema, dict):
            schema = {
                "type": "object",
                "properties": {
                    "dominant_emotion": {"type": "string"},
                    "scores": {"type": "object"},
                    "reason": {"type": "string"},
                },
                "required": ["dominant_emotion", "scores", "reason"],
            }

        try:
            payload = await self._call_img2chan_ollama_json(
                model=model_name or "default",
                system_prompt=sys_prompt,
                user_prompt=usr_prompt,
                schema=schema,
                temperature=0.35,
                num_predict=256,
            )
        except Exception as e:
            log.debug("[hutaba] 書き込み後感情のLLM生成に失敗: %s", e)
            return None

        if not isinstance(payload, dict) or not payload:
            return None

        raw_reason = re.sub(r"\s+", " ", str(payload.get("reason") or "").strip()).strip()
        # 定型プレースホルダや短すぎる理由を弾く
        if len(raw_reason) < 10 or any(
            bad in raw_reason
            for bad in ("理由を簡潔に", "具体的な理由", "JSON", "スキーマ")
        ):
            return None
        if not raw_reason.endswith(("。", "！", "!", "？", "?")):
            raw_reason += "。"
        elif not raw_reason.endswith("。"):
            raw_reason = raw_reason.rstrip("！!？?") + "。"

        valid_emotions = {
            "joy",
            "anticipation",
            "surprise",
            "trust",
            "sadness",
            "anger",
            "fear",
            "disgust",
            "neutral",
        }
        dom = str(payload.get("dominant_emotion") or "joy").strip().lower()
        if dom not in valid_emotions:
            dom = "joy"

        raw_scores = payload.get("scores")
        clean_scores: dict[str, float] = {}
        if isinstance(raw_scores, dict):
            for k in ("joy", "anticipation", "surprise", "trust", "sadness", "anger", "fear", "disgust"):
                val = raw_scores.get(k)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    clean_scores[k] = max(0.0, min(1.0, round(float(val), 4)))

        return {
            "dominant_emotion": dom,
            "scores": clean_scores,
            "reason": raw_reason,
        }

    async def _execute_img2chan_thread_post(
        self,
        message: discord.Message | None,
        url: str,
        *,
        delay_sec: float | None,
        thread_state: dict[str, Any] | None = None,
        requester_text: str = "",
        target_reply: dict[str, Any] | None = None,
        quoted_own_posts: list[dict[str, Any]] | None = None,
        report_result: bool = True,
    ) -> dict[str, Any]:
        helper = _get_img2chan_helper()
        if helper is None:
            return {
                "success": False,
                "url": url,
                "comment": "",
                "error": "img2chan helper が利用できません。",
            }

        await self._yield_to_user_replies()

        max_replies = max(self._img2chan_post_context_replies(), self._img2chan_thread_monitor_max_replies())
        timeout_sec = max(cfg_float("IMG2CHAN_POST_TIMEOUT_SEC", 20.0), 5.0)
        cached_caption = None
        if thread_state is not None and isinstance(thread_state, dict):
            cached_caption = str(thread_state.get("op_image_caption") or "").strip() or None
        try:
            try:
                thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=max_replies,
                    timeout_sec=timeout_sec,
                    cached_caption=cached_caption,
                )
            except TypeError:
                thread = await getattr(helper, "fetch_img_thread")(
                    url,
                    max_replies=max_replies,
                    timeout_sec=timeout_sec,
                )
        except Exception as e:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {e}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        error = str((thread or {}).get("error") or "").strip()
        if error:
            result = {"success": False, "url": url, "comment": "", "error": f"スレ取得に失敗しました: {error}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if thread and isinstance(thread, dict) and thread_state is not None and isinstance(thread_state, dict):
            caption = str(thread.get("op_image_caption") or "").strip()
            if caption and not thread_state.get("op_image_caption"):
                thread_state["op_image_caption"] = caption

        self._log_img2chan_thread_analysis(
            context="followup_fetch" if target_reply is not None else "initial_fetch",
            url=url,
            thread=thread,
            message=message,
        )

        if thread_state is not None:
            self._img2chan_track_known_replies(thread_state, thread)

        own_nos_existing = thread_state.get("own_post_nos") or set() if thread_state else set()
        if thread_state and not bool(thread_state.get("user_requested")) and target_reply is None and not own_nos_existing:
            request_reason = self._is_img2chan_request_thread(thread)
            if request_reason:
                log.info(
                    "[hutaba] リクエスト系スレッドのため自発書き込みをスキップします: %s (reason=%s)",
                    url,
                    request_reason,
                )
                thread_state["status"] = "aborted"
                return {
                    "success": False,
                    "url": url,
                    "error": f"request_thread_detected:{request_reason}",
                }
            expire_reason = self._is_hutaba_thread_expiring_soon(thread)
            if expire_reason:
                log.info(
                    "[hutaba] スレッドが消滅間近（10分以内等）のため自発書き込みをスキップします: %s (reason=%s expire_text=%s)",
                    url,
                    expire_reason,
                    str((thread or {}).get("expire_text") or ""),
                )
                thread_state["status"] = "completed"
                return {
                    "success": False,
                    "url": url,
                    "error": f"thread_expiring_soon:{expire_reason}",
                }

        await self._yield_to_user_replies()

        # 進行中スレッド（10レス以上）での事前返信先確定ガード
        effective_target_reply = target_reply
        replies_list = list(thread.get("replies") or [])
        total_replies = int(thread.get("total_reply_count") or len(replies_list))
        is_active_thread = total_replies >= 10 or len(replies_list) >= 10

        if effective_target_reply is None and is_active_thread:
            counter_fn = getattr(helper, "calculate_img_thread_reply_counts", None)
            reply_counts = counter_fn(thread) if callable(counter_fn) else {}
            best_hot_post: dict[str, Any] | None = None
            best_cnt = 0
            recent_window = [r for r in replies_list[-20:] if isinstance(r, dict)]
            # 直近20件以内のレスのみを事前ターゲット候補とする（古い1件被引用レスへの誤固定を防止）
            for cand in reversed(recent_window):
                if _has_hutaba_unsupported_media(str(cand.get("text") or cand.get("body") or "")):
                    continue
                c_no = _normalize_img_post_no(cand.get("post_no"))
                p_no_int = int(cand.get("post_no") or cand.get("number") or 0)
                cnt = int(reply_counts.get(c_no, 0) or reply_counts.get(p_no_int, 0) or 0)
                if cnt > best_cnt:
                    body_preview = str(cand.get("text") or cand.get("body") or "").strip()
                    if len(body_preview) >= 4:
                        best_cnt = cnt
                        best_hot_post = cand

            # 直近20件に被返信レスがない場合、全体からのフォールバックは cnt >= 2 の盛り上がりレスに限定
            if best_hot_post is None and reply_counts:
                for cand in reversed(replies_list):
                    if not isinstance(cand, dict) or _has_hutaba_unsupported_media(str(cand.get("text") or cand.get("body") or "")):
                        continue
                    c_no = _normalize_img_post_no(cand.get("post_no"))
                    p_no_int = int(cand.get("post_no") or cand.get("number") or 0)
                    cnt = int(reply_counts.get(c_no, 0) or reply_counts.get(p_no_int, 0) or 0)
                    if cnt >= 2 and cnt > best_cnt:
                        body_preview = str(cand.get("text") or cand.get("body") or "").strip()
                        if len(body_preview) >= 4:
                            best_cnt = cnt
                            best_hot_post = cand

            if best_hot_post is not None:
                effective_target_reply = best_hot_post
                log.info(
                    "[hutaba] 進行中スレのため最多被返信レスを事前ターゲットに確定: post_no=%s count=%d url=%s",
                    best_hot_post.get("post_no"),
                    best_cnt,
                    url,
                )

        try:
            comment, quote_post_numbers, quote_reply_numbers = await self._generate_hutaba_reply(
                message,
                url,
                thread=thread,
                thread_state=thread_state if thread_state is not None else {},
                target_reply=effective_target_reply,
                quoted_own_posts=quoted_own_posts,
                requester_text=requester_text,
            )
        except Exception as e:
            reason = _compact_exception_message(e)
            log.warning(
                "[hutaba] スレッド返信の生成に失敗したため投稿を中止しました: url=%s error=%s",
                url,
                reason,
            )
            result = {"success": False, "posted": False, "url": url, "comment": "", "error": f"投稿文生成に失敗しました: {reason}"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "posted": False, "url": url, "comment": comment, "error": "投稿本文を作れませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        if hasattr(helper, "compose_img_thread_reply_comment"):
            post_plan = getattr(helper, "compose_img_thread_reply_comment")(
                thread,
                comment,
                quote_post_numbers=quote_post_numbers,
                quote_reply_numbers=quote_reply_numbers,
                max_chars=340,
            )
            comment = str(post_plan.get("comment") or comment).strip()

        promotion_applied = False
        promotion_invite_url = ""
        comment, promotion_applied, promotion_invite_url = self._maybe_append_img2chan_promotion(
            helper,
            comment,
        )

        if not comment or looks_like_abnormal_assistant_reply(comment):
            result = {"success": False, "posted": False, "url": url, "comment": comment, "error": "投稿本文を整形できませんでした。"}
            if report_result:
                await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
            return result

        await self._yield_to_user_replies()

        is_dry_run = bool(cfg_bool("IMG2CHAN_POST_DRY_RUN", False))
        log.info(
            "[hutaba] スレッド書き込み開始: url=%s dry_run=%s comment=%r",
            url,
            is_dry_run,
            comment,
        )

        if is_dry_run:
            result = {
                "success": True,
                "posted": True,
                "dry_run": True,
                "url": url,
                "comment": comment,
                "error": "",
            }
        else:
            result = await getattr(helper, "submit_img_thread_reply")(
                url,
                comment,
                email=str(cfg("IMG2CHAN_POST_EMAIL", "") or ""),
                password=str(cfg("IMG2CHAN_POST_PASSWORD", "") or ""),
                timeout_sec=timeout_sec,
                preserve_urls=[promotion_invite_url] if promotion_applied and promotion_invite_url else None,
            )
            result["posted"] = bool(result.get("success"))

        if promotion_applied and (
            bool(result.get("dry_run"))
            or bool(result.get("success"))
            or bool(result.get("submitted"))
        ):
            self._mark_img2chan_promotion_sent()

        if bool(result.get("success")):
            log.info(
                "[hutaba] スレッド書き込み成功: url=%s dry_run=%s comment=%r",
                url,
                bool(result.get("dry_run")),
                comment,
            )
            if thread_state is not None:
                thread_state["last_own_post_at"] = time.time()

            # === 全フロント共通行動ログへの記録 ===
            memory_store = getattr(self, "memory_store", None)
            if memory_store is not None and bool(cfg("ACTIVITY_LOG_ENABLED", True)):
                add_act_fn = getattr(memory_store, "add_bot_activity", None)
                if callable(add_act_fn):
                    try:
                        from ollama_bot.common.config_helpers import cfg_persona_namespace
                        # スレッドURLから板名とスレ番号を抽出して source_ref を生成
                        source_ref = "img"
                        m_ref = _IMG_THREAD_URL_PATTERN.search(url)
                        if m_ref:
                            board = str(m_ref.group("server") or "img").lower()
                            t_no = str(m_ref.group("thread_no") or "")
                            source_ref = f"{board}/{t_no}" if t_no else f"img/{m_ref.group(1)}"

                        # 引用・レス先があれば80字以内で detail に記録
                        reply_target_detail: str | None = None
                        if effective_target_reply and isinstance(effective_target_reply, dict):
                            t_body = str(effective_target_reply.get("text") or "").strip().replace("\n", " ")
                            if t_body:
                                reply_target_detail = f"レス先: {t_body[:80]}"

                        await add_act_fn(
                            persona=cfg_persona_namespace(custom_cfg=cfg),
                            front="futaba",
                            kind="post",
                            summary=str(comment or "").strip()[:400],
                            detail=reply_target_detail,
                            guild_id=None,
                            channel_id=None,
                            source_ref=source_ref,
                        )
                    except Exception as act_e:
                        log.debug("failed to record futaba post activity: %s", act_e)

        if bool(result.get("success")):
            try:
                llm_emotion = await self._generate_hutaba_post_emotion_via_llm(
                    url=url,
                    thread=thread,
                    comment=comment,
                    target_reply=effective_target_reply,
                )
                emotion_state = getattr(self, "emotion_state", None)
                if emotion_state is not None and hasattr(emotion_state, "scores") and isinstance(emotion_state.scores, dict):
                    scores = emotion_state.scores
                    for k in list(scores.keys()):
                        scores[k] = round(float(scores.get(k, 0.0) or 0.0) * 0.75, 4)

                    if isinstance(llm_emotion, dict):
                        llm_scores = llm_emotion.get("scores")
                        if isinstance(llm_scores, dict) and llm_scores:
                            for k, v in llm_scores.items():
                                if k in scores or k in ("joy", "anticipation", "surprise", "trust", "sadness", "anger", "fear", "disgust"):
                                    scores[k] = min(1.0, round(float(scores.get(k, 0.0) or 0.0) + float(v) * 0.5, 4))
                        dom_llm = str(llm_emotion.get("dominant_emotion") or "joy")
                        if dom_llm != "neutral":
                            scores[dom_llm] = max(float(scores.get(dom_llm, 0.0) or 0.0), 0.35)
                        dominant = max(scores.items(), key=lambda x: float(x[1]))[0] if scores else dom_llm
                        if float(scores.get(dominant, 0.0) or 0.0) < 0.25:
                            dominant = "neutral"
                        emotion_state.dominant_emotion = dominant
                        emotion_state.reason = str(llm_emotion["reason"])
                    else:
                        scores["joy"] = min(1.0, round(float(scores.get("joy", 0.0) or 0.0) + 0.30, 4))
                        if "anticipation" in scores:
                            scores["anticipation"] = min(1.0, round(float(scores.get("anticipation", 0.0) or 0.0) + 0.20, 4))
                        elif "fun" in scores:
                            scores["fun"] = min(1.0, round(float(scores.get("fun", 0.0) or 0.0) + 0.20, 4))
                        dominant = max(scores.items(), key=lambda x: float(x[1]))[0] if scores else "joy"
                        if float(scores.get(dominant, 0.0) or 0.0) < 0.25:
                            dominant = "neutral"
                        emotion_state.dominant_emotion = dominant
                        emotion_state.reason = _build_hutaba_post_emotion_reason(
                            thread,
                            comment,
                            target_reply=effective_target_reply,
                            dominant_emotion=dominant,
                            url=url,
                        )

                    recompute_fn = getattr(self, "_recompute_composite_indices", None)
                    if callable(recompute_fn):
                        with contextlib.suppress(Exception):
                            recompute_fn()
                    save_fn = getattr(self, "_save_emotion_state", None)
                    if callable(save_fn):
                        save_fn()
                else:
                    update_emotion = getattr(self, "_update_emotion_state", None)
                    fallback_reason = (
                        str(llm_emotion["reason"])
                        if isinstance(llm_emotion, dict) and llm_emotion.get("reason")
                        else _build_hutaba_post_emotion_reason(
                            thread,
                            comment,
                            target_reply=effective_target_reply,
                            dominant_emotion="joy",
                            url=url,
                        )
                    )
                    changes_dict = (
                        dict(llm_emotion["scores"])
                        if isinstance(llm_emotion, dict) and isinstance(llm_emotion.get("scores"), dict) and llm_emotion["scores"]
                        else {"joy": 0.3, "fun": 0.2}
                    )
                    if callable(update_emotion):
                        try:
                            await update_emotion(
                                changes=changes_dict,
                                reason=fallback_reason,
                            )
                        except TypeError:
                            pass
                sync_presence = getattr(self, "_sync_emotion_presence", None)
                if callable(sync_presence):
                    await sync_presence(force=True)
            except Exception as e:
                log.debug("[hutaba] 投稿成功時の感情更新をスキップ: %s", e)
        else:
            log.warning(
                "[hutaba] スレッド書き込み失敗: url=%s err=%s comment=%r",
                url,
                result.get("error") or "unknown",
                comment,
            )

        if bool(result.get("success")) and not bool(result.get("dry_run")) and thread_state is not None:
            self._remember_img2chan_pending_own_post(thread_state, comment)
            known_before_after = set(thread_state.get("known_reply_post_nos", set()) or [])
            try:
                cached_caption = str(thread_state.get("op_image_caption") or (thread or {}).get("op_image_caption") or "").strip() or None
                try:
                    after_thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=self._img2chan_thread_monitor_max_replies(),
                        timeout_sec=timeout_sec,
                        cached_caption=cached_caption,
                    )
                except TypeError:
                    after_thread = await getattr(helper, "fetch_img_thread")(
                        url,
                        max_replies=self._img2chan_thread_monitor_max_replies(),
                        timeout_sec=timeout_sec,
                    )
            except Exception as e:
                log.warning("[hutaba] 投稿後スレッド再取得失敗 url=%s err=%r", url, e)
            else:
                if after_thread and isinstance(after_thread, dict):
                    caption = str(after_thread.get("op_image_caption") or "").strip()
                    if caption and not thread_state.get("op_image_caption"):
                        thread_state["op_image_caption"] = caption
                after_error = str((after_thread or {}).get("error") or "").strip()
                if not after_error:
                    matched_reply = getattr(helper, "find_img_thread_reply_by_comment")(
                        after_thread,
                        comment,
                        exclude_post_numbers=known_before_after,
                    ) if hasattr(helper, "find_img_thread_reply_by_comment") else None
                    self._img2chan_track_known_replies(thread_state, after_thread)
                    if matched_reply:
                        self._register_img2chan_own_post(thread_state, matched_reply)
                        result["registered_post_no"] = str(matched_reply.get("post_no") or "").strip()
                        log.info(
                            "[hutaba] スレッド書き込み反映確認: url=%s post_no=%s comment=%r",
                            url,
                            result["registered_post_no"],
                            comment,
                        )
                    else:
                        matched_replies = self._match_img2chan_pending_own_posts(thread_state, after_thread)
                        if matched_replies:
                            result["registered_post_no"] = str((matched_replies[-1] or {}).get("post_no") or "").strip()
                            log.info(
                                "[hutaba] スレッド保留書き込み反映確認: url=%s post_no=%s comment=%r",
                                url,
                                result["registered_post_no"],
                                comment,
                            )
            await self._store_img2chan_post_record(
                message,
                url=url,
                comment=comment,
                target_reply=target_reply,
                registered_post_no=str(result.get("registered_post_no") or "").strip(),
                dry_run=bool(result.get("dry_run")),
            )

        if report_result:
            await self._send_img2chan_post_report(message, result, delay_sec=delay_sec)
        return result

    async def _send_img2chan_post_report(
        self,
        message: discord.Message | None,
        post_result: dict[str, Any],
        *,
        delay_sec: float | None = None,
    ) -> None:
        if message is None:
            return
        if not cfg_bool("IMG2CHAN_POST_REPORT_ENABLED", False):
            return
        helper = _get_img2chan_helper()
        if helper is not None and hasattr(helper, "format_img_post_discord_report"):
            report = getattr(helper, "format_img_post_discord_report")(post_result, delay_sec=delay_sec)
        else:
            result = dict(post_result or {})
            report = str(result.get("error") or "imgの書き込み処理が終わりました。")
        report = truncate_text(str(report or "").strip(), 1900)
        if not report:
            return
        channel = getattr(message, "channel", None)
        if channel is None or not hasattr(channel, "send"):
            return
        await channel.send(report, allowed_mentions=discord.AllowedMentions.none())


Img2ChanPostingMixin = _Img2chanPostingMixin

