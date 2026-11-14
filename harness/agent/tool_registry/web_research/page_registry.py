"""web_research/page_registry.py

web_run ツール（adapters/web_run.py）向けのページ保持レジストリ。

- 検索結果（ref_id: s0, s1, ...）と取得済みページ本文（ref_id: p0, p1, ...）を
  スコープ（会話）単位でプロセス内メモリにのみ保持する（永続化しない）。
- 上限: スコープあたりページ20件・検索結果40件（超過時は古いものから FIFO で破棄）、
  1ページ本文は最大60,000字で切り詰める。
- 有効期限: 最終アクセスから30分でスコープごと破棄する。常駐タスクは持たず、
  各メソッド呼び出し時に期限切れスコープを掃除する。
- ページ内検索（find_in_page）は LLM を使わない決定論的な部分一致のみ。
"""
from __future__ import annotations

import logging
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("harness.agent.tool_registry.web_research.page_registry")

DEFAULT_MAX_PAGES_PER_SCOPE = 20
DEFAULT_MAX_SEARCH_RESULTS_PER_SCOPE = 40
DEFAULT_MAX_PAGE_CHARS = 60_000
DEFAULT_TTL_SEC = 30 * 60
DEFAULT_MAX_FIND_RESULTS = 20
# 1行が長すぎると行番号での参照が効かないため、保持時にこの字数で折り返す
DEFAULT_MAX_LINE_CHARS = 200

_SENTENCE_BREAK_CHARS = ("。", "！", "？", "!", "?", "．", "\u3000")


def _normalize_for_match(text: str) -> str:
    """全角/半角・大文字/小文字の揺れを吸収した照合用文字列を返す（決定論的）。"""
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _wrap_long_line(line: str, max_chars: int) -> list[str]:
    """max_chars を超える行を、文末記号・空白の直後を優先して折り返す。"""
    if len(line) <= max_chars:
        return [line]
    chunks: list[str] = []
    rest = line
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = -1
        for ch in _SENTENCE_BREAK_CHARS:
            pos = window.rfind(ch)
            if pos > cut:
                cut = pos
        space_pos = window.rfind(" ")
        if space_pos > cut:
            cut = space_pos
        # 極端に短い断片になる区切りは採用せずハードカットする
        if cut < max_chars // 3:
            cut = max_chars - 1
        piece = rest[: cut + 1].strip()
        if piece:
            chunks.append(piece)
        rest = rest[cut + 1 :].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return chunks


def split_page_lines(text: str, *, max_line_chars: int = DEFAULT_MAX_LINE_CHARS) -> list[str]:
    """ページ本文を行に分割する。空行は除去し、長い行は max_line_chars で折り返す。"""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        lines.extend(_wrap_long_line(stripped, max(int(max_line_chars), 20)))
    return lines


@dataclass
class _ScopeState:
    last_access: float
    search_hits: "OrderedDict[str, dict[str, Any]]" = field(default_factory=OrderedDict)
    pages: "OrderedDict[str, dict[str, Any]]" = field(default_factory=OrderedDict)
    next_search_index: int = 0
    next_page_index: int = 0


class PageRegistry:
    """スコープ（会話）ごとに検索結果とページ本文を保持するプロセス内レジストリ。"""

    def __init__(
        self,
        *,
        max_pages: int = DEFAULT_MAX_PAGES_PER_SCOPE,
        max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS_PER_SCOPE,
        max_page_chars: int = DEFAULT_MAX_PAGE_CHARS,
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_find_results: int = DEFAULT_MAX_FIND_RESULTS,
        max_line_chars: int = DEFAULT_MAX_LINE_CHARS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_pages = max(int(max_pages), 1)
        self.max_search_results = max(int(max_search_results), 1)
        self.max_page_chars = max(int(max_page_chars), 1)
        self.ttl_sec = float(ttl_sec)
        self.max_find_results = max(int(max_find_results), 1)
        self.max_line_chars = max(int(max_line_chars), 20)
        self._clock: Callable[[], float] = clock or time.monotonic
        self._scopes: dict[str, _ScopeState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 内部ヘルパー（呼び出し側で self._lock を保持していること）
    # ------------------------------------------------------------------
    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            name for name, state in self._scopes.items() if now - state.last_access >= self.ttl_sec
        ]
        for name in expired:
            del self._scopes[name]
        if expired:
            log.info("[page-registry] Expired scopes purged count=%d", len(expired))

    def _get_scope_locked(self, scope: str, *, create: bool) -> _ScopeState | None:
        now = self._clock()
        self._purge_expired_locked(now)
        key = str(scope or "default")
        state = self._scopes.get(key)
        if state is None:
            if not create:
                return None
            state = _ScopeState(last_access=now)
            self._scopes[key] = state
        state.last_access = now
        return state

    # ------------------------------------------------------------------
    # 公開 API（契約）
    # ------------------------------------------------------------------
    def register_search_results(self, scope: str, results: list[dict[str, Any]]) -> list[str]:
        """各検索結果に ref_id（s0, s1, ...）を割り当てて URL とタイトルを保持し、ref_id のリストを返す。

        任意キー `snippet` と、検索時にブラウザで既読の本文 `content_text`（あれば）も保持する。
        """
        with self._lock:
            state = self._get_scope_locked(scope, create=True)
            assert state is not None
            ref_ids: list[str] = []
            for item in results or []:
                if not isinstance(item, dict):
                    item = {}
                ref_id = f"s{state.next_search_index}"
                state.next_search_index += 1
                content_text = str(item.get("content_text") or "")
                state.search_hits[ref_id] = {
                    "url": str(item.get("url") or item.get("href") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip(),
                    "content_text": content_text[: self.max_page_chars],
                }
                ref_ids.append(ref_id)
                while len(state.search_hits) > self.max_search_results:
                    dropped, _ = state.search_hits.popitem(last=False)
                    log.debug("[page-registry] Search hit evicted scope=%s ref_id=%s", scope, dropped)
            return ref_ids

    def register_page(self, scope: str, url: str, title: str, text: str) -> str:
        """ページ本文を行に分割して保持し、ref_id（p0, p1, ...）を返す。本文は1ページ最大60,000字で切り詰める。"""
        raw_text = str(text or "")
        truncated = len(raw_text) > self.max_page_chars
        body = raw_text[: self.max_page_chars]
        lines = split_page_lines(body, max_line_chars=self.max_line_chars)
        with self._lock:
            state = self._get_scope_locked(scope, create=True)
            assert state is not None
            ref_id = f"p{state.next_page_index}"
            state.next_page_index += 1
            state.pages[ref_id] = {
                "url": str(url or "").strip(),
                "title": str(title or "").strip(),
                "lines": lines,
                "truncated": truncated,
            }
            while len(state.pages) > self.max_pages:
                dropped, _ = state.pages.popitem(last=False)
                log.info("[page-registry] Page evicted (FIFO) scope=%s ref_id=%s", scope, dropped)
            return ref_id

    def resolve(self, scope: str, ref_id: str) -> dict[str, Any] | None:
        """ref_id が検索結果なら {"kind": "search_hit", "url", "title"}、ページなら {"kind": "page", "url", "title", "lines"} を返す。

        検索結果には補助キー `snippet` / `content_text`、ページには `truncated` が付く。未登録・期限切れは None。
        """
        key = str(ref_id or "").strip()
        with self._lock:
            state = self._get_scope_locked(scope, create=False)
            if state is None:
                return None
            hit = state.search_hits.get(key)
            if hit is not None:
                return {"kind": "search_hit", **hit}
            page = state.pages.get(key)
            if page is not None:
                return {
                    "kind": "page",
                    "url": page["url"],
                    "title": page["title"],
                    "lines": list(page["lines"]),
                    "truncated": bool(page.get("truncated")),
                }
            return None

    def find_in_page(self, scope: str, ref_id: str, pattern: str) -> list[dict[str, Any]]:
        """ページ本文から pattern を含む行を探し、[{"lineno": int, "line": str}] を返す（最大20件、決定論的処理のみ）。

        照合は NFKC 正規化＋大文字小文字無視の部分一致。行番号は1始まり。
        """
        needle = _normalize_for_match(pattern).strip()
        if not needle:
            return []
        with self._lock:
            state = self._get_scope_locked(scope, create=False)
            if state is None:
                return []
            page = state.pages.get(str(ref_id or "").strip())
            if page is None:
                return []
            matches: list[dict[str, Any]] = []
            for idx, line in enumerate(page["lines"], start=1):
                if needle in _normalize_for_match(line):
                    matches.append({"lineno": idx, "line": line})
                    if len(matches) >= self.max_find_results:
                        break
            return matches

    # ------------------------------------------------------------------
    # 補助 API
    # ------------------------------------------------------------------
    def find_page_by_url(self, scope: str, url: str) -> str | None:
        """同じ URL の保持済みページがあれば最新の ref_id を返す（再取得の抑止用）。"""
        target = str(url or "").strip()
        if not target:
            return None
        with self._lock:
            state = self._get_scope_locked(scope, create=False)
            if state is None:
                return None
            for ref_id in reversed(list(state.pages.keys())):
                if state.pages[ref_id]["url"] == target:
                    return ref_id
            return None

    def clear(self, scope: str | None = None) -> None:
        """指定スコープ（None なら全スコープ）を破棄する。"""
        with self._lock:
            if scope is None:
                self._scopes.clear()
            else:
                self._scopes.pop(str(scope), None)


default_registry = PageRegistry()


def get_default_page_registry() -> PageRegistry:
    """モジュールレベルの単一インスタンスを返す（テストでは default_registry を差し替え可能）。"""
    return default_registry


__all__ = [
    "PageRegistry",
    "default_registry",
    "get_default_page_registry",
    "split_page_lines",
]
