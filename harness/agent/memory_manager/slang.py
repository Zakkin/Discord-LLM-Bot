"""
[AI Agent Summary]
掲示板・ネットスラングの辞書定義、SQLite (memory.db) 永続化・自動シード、照合・フィルタリングモジュール。
This module manages BBS/net slang definitions, DB persistence, automated seeding, text matching, and query sanitization.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, TypedDict

log = logging.getLogger("harness.agent.memory_manager.slang")


class SlangEntry(TypedDict, total=False):
    term: str
    category: str  # "prefix_slang", "reaction_meme", "community_term", "archetype", "slang"
    meaning: str
    strip_from_query: bool
    is_proper_noun: bool
    guidance: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 充実したふたば・掲示板スラングのデフォルトマスター辞書（20選）
DEFAULT_BBS_SLANG_DICT: dict[str, SlangEntry] = {
    # === [接頭辞・前置き系: 検索クエリから必ず除外] ===
    "すみフラ": {
        "term": "すみフラ",
        "category": "prefix_slang",
        "meaning": "「すみませんフライングですが質問します」の略。質問や相談の前置き",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "キャラクター名や作品名ではなく、質問・相談の前置き略語です。話題の対象・固有名詞として扱わないこと。",
    },
    "うろフラ": {
        "term": "うろフラ",
        "category": "prefix_slang",
        "meaning": "「うろ覚えフライング」の略。うろ覚えな話題を切り出す前置き",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "キャラクター名や作品名ではなく、前置き略語です。",
    },
    "フラ": {
        "term": "フラ",
        "category": "prefix_slang",
        "meaning": "「フライング（先行質問・話題投下）」の略",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "文脈がフライング質問の場合、フラダンス等の別概念と混同しないよう注意してください。",
    },
    "チラ裏": {
        "term": "チラ裏",
        "category": "prefix_slang",
        "meaning": "「チラシの裏にでも書いてろ」から転じた、独り言や個人的な雑談の前置き",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "個人的な呟きや独り言の前置きです。",
    },
    "自レス": {
        "term": "自レス",
        "category": "prefix_slang",
        "meaning": "自分の投稿に対する自己返信・追記",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "自己レスポンスの前置きです。",
    },
    "連投すまぬ": {
        "term": "連投すまぬ",
        "category": "prefix_slang",
        "meaning": "連続投稿の謝罪・前置き",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "連続投稿の挨拶です。",
    },

    # === [定型リアクション・相槌・ツッコミ系] ===
    "なそ": {
        "term": "なそ",
        "category": "reaction_meme",
        "meaning": "「なんやそれ（驚き・呆れ）」を意味する定型ツッコミ",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "驚きや呆れを表す定型ツッコミです。",
    },
    "にん": {
        "term": "にん",
        "category": "reaction_meme",
        "meaning": "「せやな・確かに」などの肯定・同意を表す定型相槌",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "肯定や同意を表す定型相槌です。",
    },
    "そうだね": {
        "term": "そうだね",
        "category": "reaction_meme",
        "meaning": "賛同・共感を表す定型句、または掲示板の賛同ボタン機能",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "共感・賛同の定型表現です。",
    },
    "うｎ": {
        "term": "うｎ",
        "category": "reaction_meme",
        "meaning": "「うん（頷き・同意）」のふたば風表記",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "静かな頷きや同意を表す相槌です。",
    },
    "そらそうよ": {
        "term": "そらそうよ",
        "category": "reaction_meme",
        "meaning": "当然の結果や因果応報に対する定型ツッコミ",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "当然の結果に対するツッコミです。",
    },
    "お前が始めた物語だろ": {
        "term": "お前が始めた物語だろ",
        "category": "reaction_meme",
        "meaning": "自分の行動が原因で招いた因果応報・自業自得に対する定番ツッコミ",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "自業自得な状況に対するツッコミ定型句です。",
    },
    "だが待って欲しい": {
        "term": "だが待って欲しい",
        "category": "reaction_meme",
        "meaning": "逆張りや冷静なツッコミを切り出す前置き",
        "strip_from_query": True,
        "is_proper_noun": False,
        "guidance": "再考や逆の視点を促すツッコミの前置きです。",
    },

    # === [コミュニティ呼称・主語・システム系] ===
    "「」": {
        "term": "「」",
        "category": "community_term",
        "meaning": "ふたば☆ちゃんねる img 板における名無し住人（としあき/けっこん）の呼称",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "掲示板の仲間・住人を指す代名詞です。引用符ではなく『住人』の意味として扱ってください。",
    },
    "としあき": {
        "term": "としあき",
        "category": "community_term",
        "meaning": "ふたば☆ちゃんねるの利用者の総称",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "掲示板の利用者全般を指す呼称です。",
    },
    "ッチー": {
        "term": "ッチー",
        "category": "community_term",
        "meaning": "ChatGPTなどの対話型AIツールの俗称",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "対話型AIサービスの俗称です。",
    },
    "チャッピー": {
        "term": "チャッピー",
        "category": "community_term",
        "meaning": "ChatGPT等のAIアシスタントの俗称",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "AIアシスタントの愛称・俗称です。",
    },
    "定時": {
        "term": "定時",
        "category": "community_term",
        "meaning": "毎日決まった時間・形式で進行する定期スレッド",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "定期的に立つお決まりのスレッドを指します。",
    },

    # === [キャラクター属性・AI生成ルール系] ===
    "ロリババア": {
        "term": "ロリババア",
        "category": "archetype",
        "meaning": "外見は幼く設定上は高齢・長命なキャラクター属性",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "外見と設定年齢にギャップがある属性です。AI生成やセーフティ判定の可否において、設定年齢とアバター表現の工夫で議論されます。",
    },
    "メスガキ": {
        "term": "メスガキ",
        "category": "archetype",
        "meaning": "生意気で挑発的な言動をとる少女キャラクター属性",
        "strip_from_query": False,
        "is_proper_noun": False,
        "guidance": "生意気な態度をとるキャラクタージャンルです。プロンプトやフィルターの適用範囲で話題になります。",
    },
}

# インメモリキャッシュ（高速マッチング用）
_SLANG_CACHE: dict[str, SlangEntry] = dict(DEFAULT_BBS_SLANG_DICT)


async def seed_default_slangs(store: Any) -> int:
    """memory.db の bbs_slangs テーブルにデフォルト辞書を INSERT OR IGNORE で一括投入する。"""
    if store is None or not hasattr(store, "_connect"):
        return 0
    now = _now_iso()
    count = 0
    conn = await store._connect()
    try:
        cur = await conn.cursor()
        for term, entry in DEFAULT_BBS_SLANG_DICT.items():
            await cur.execute(
                """
                INSERT OR IGNORE INTO bbs_slangs (
                    term, category, meaning, strip_from_query, is_proper_noun, guidance, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'default', ?, ?)
                """,
                (
                    term,
                    entry["category"],
                    entry["meaning"],
                    1 if entry.get("strip_from_query") else 0,
                    1 if entry.get("is_proper_noun") else 0,
                    entry["guidance"],
                    now,
                    now,
                ),
            )
            if cur.rowcount > 0:
                count += 1
        await conn.commit()
    except Exception as e:
        log.warning("seed_default_slangs failed: %r", e)
    finally:
        await conn.close()
    return count


async def load_slangs_from_db(store: Any) -> dict[str, SlangEntry]:
    """DBからスラング一覧を読み出し、インメモリキャッシュを同期する。"""
    if store is None or not hasattr(store, "_connect"):
        return _SLANG_CACHE
    try:
        conn = await store._connect()
        try:
            cur = await conn.cursor()
            await cur.execute("SELECT term, category, meaning, strip_from_query, is_proper_noun, guidance FROM bbs_slangs")
            rows = await cur.fetchall()
            for r in rows:
                _SLANG_CACHE[r[0]] = {
                    "term": r[0],
                    "category": r[1],
                    "meaning": r[2],
                    "strip_from_query": bool(r[3]),
                    "is_proper_noun": bool(r[4]),
                    "guidance": r[5],
                }
        finally:
            await conn.close()
    except Exception as e:
        log.warning("load_slangs_from_db failed: %r", e)
    return _SLANG_CACHE


async def upsert_slang(store: Any, entry: SlangEntry, source: str = "manual") -> bool:
    """スラングをDBに新規登録または更新する。"""
    term = entry.get("term", "").strip()
    if not term or store is None or not hasattr(store, "_connect"):
        return False
    now = _now_iso()
    try:
        conn = await store._connect()
        try:
            cur = await conn.cursor()
            await cur.execute(
                """
                INSERT INTO bbs_slangs (
                    term, category, meaning, strip_from_query, is_proper_noun, guidance, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(term) DO UPDATE SET
                    category=excluded.category,
                    meaning=excluded.meaning,
                    strip_from_query=excluded.strip_from_query,
                    is_proper_noun=excluded.is_proper_noun,
                    guidance=excluded.guidance,
                    updated_at=excluded.updated_at
                """,
                (
                    term,
                    entry.get("category", "slang"),
                    entry.get("meaning", ""),
                    1 if entry.get("strip_from_query") else 0,
                    1 if entry.get("is_proper_noun") else 0,
                    entry.get("guidance", ""),
                    source,
                    now,
                    now,
                ),
            )
            await conn.commit()
            _SLANG_CACHE[term] = entry
            return True
        finally:
            await conn.close()
    except Exception as e:
        log.warning("upsert_slang failed: %r", e)
        return False


def lookup_slang_in_text(text: str) -> list[SlangEntry]:
    """テキスト内に含まれる登録スラングを検出し、その辞書エントリ一覧を返す（文字数降順マッチ）。"""
    if not text:
        return []
    matched: list[SlangEntry] = []
    seen_terms: set[str] = set()

    sorted_terms = sorted(_SLANG_CACHE.keys(), key=len, reverse=True)
    for term in sorted_terms:
        if term in text and term not in seen_terms:
            seen_terms.add(term)
            matched.append(_SLANG_CACHE[term])
    return matched


def filter_query_terms_by_slang(terms: list[str]) -> list[str]:
    """検索クエリ候補リストから、strip_from_query=True や prefix_slang の単語を除外する。"""
    if not terms:
        return []
    filtered: list[str] = []
    for t in terms:
        t_clean = t.strip()
        entry = _SLANG_CACHE.get(t_clean)
        if entry:
            if entry.get("strip_from_query", False) or entry.get("category") == "prefix_slang":
                continue
        # 部分一致で接頭辞が含まれる複合トークンも除外
        if any(p in t_clean for p in ("すみフラ", "うろフラ")):
            continue
        filtered.append(t)
    return filtered


def format_slang_guidance_for_prompt(entries: list[SlangEntry]) -> list[str]:
    """検出されたスラング一覧から、プロンプトに注入する注意書きテキスト行を生成する。"""
    if not entries:
        return []
    lines: list[str] = []
    for e in entries:
        term = e.get("term", "")
        guidance = e.get("guidance") or e.get("meaning", "")
        if term and guidance:
            lines.append(f"- 『{term}』: {guidance}")
    return lines
