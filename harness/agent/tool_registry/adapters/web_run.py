"""AIエージェント・ハーネス バッチリサーチツールアダプター (adapters/web_run.py)

OpenAI Codex の独立型検索ツール `web.run` のプロトコル（コマンドの束ね＋参照ID＋行番号つきページ）を、
既存の Chrome MCP 系ディスパッチ（browser_search / browser_read_url）の上に移植したツール `web_run`。

- search_query: 検索（最大4件）。結果に参照ID s0, s1, ... を付けてページレジストリへ登録する
- open: 検索結果（sN）・保持済みページ（pN）・URL 直指定を行番号つきで開く（`lineno` 指定でその行を中心に表示）
- find: ページ内の決定論的な部分一致検索（未取得なら先に取得する）
- response_length: short / medium（既定）/ long で出力量を制御する

既存の `search_web`（adapters/web_search.py）には一切手を加えない（互換維持・段階移行）。
ページ本文の保持は web_research/page_registry.py のプロセス内メモリのみ（永続化・常駐タスクなし）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlparse

from ..types import ToolCategory, ToolDefinition, ToolParameter
from .web_search import extract_partner_names, is_partner_name_query

if TYPE_CHECKING:
    from ..web_research.page_registry import PageRegistry

log = logging.getLogger("harness.agent.tool_registry.adapters.web_run")

TOOL_NAME = "web_run"
MAX_SEARCH_QUERIES = 4
MAX_OPEN_COMMANDS = 8
MAX_FIND_COMMANDS = 8
MAX_DOMAINS_PER_QUERY = 5
DEFAULT_TOOL_TIMEOUT_SEC = 240.0
# ToolExecutor のタイムアウトで結果全体が失われないよう、内部の締め切りはツールタイムアウトより短くとる
_TIMEOUT_MARGIN_SEC = 15.0

_SEARCH_REF_RE = re.compile(r"s\d+")
_PAGE_REF_RE = re.compile(r"p\d+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_DOMAIN_RE = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_SCOPE_KEYS = ("scope", "conversation_id", "channel_id", "thread_id", "session_id")
_SEARCH_FALLBACK_KEYS = ("query", "queries", "q", "search_queries")

_TIMEOUT_MESSAGE = "時間切れのため未実行です（1回の呼び出しで束ねるコマンドを減らしてください）。"


@dataclass(frozen=True)
class _LengthProfile:
    name: str
    total_chars: int
    page_lines: int
    hits_per_query: int
    snippet_chars: int
    summary_chars: int


_LENGTH_PROFILES: dict[str, _LengthProfile] = {
    # short: 抜粋のみ（現行 search_web 相当）
    "short": _LengthProfile("short", total_chars=2_000, page_lines=40, hits_per_query=3, snippet_chars=200, summary_chars=300),
    "medium": _LengthProfile("medium", total_chars=8_000, page_lines=120, hits_per_query=5, snippet_chars=300, summary_chars=600),
    "long": _LengthProfile("long", total_chars=20_000, page_lines=300, hits_per_query=8, snippet_chars=500, summary_chars=1_200),
}
DEFAULT_RESPONSE_LENGTH = "medium"


def build_partner_guard_message(name: str) -> str:
    """search_web の User-First Resolution と同一文面のガード文を返す。"""
    return (
        f"『{name}』は現在の対話相手（Discordユーザー）の名前であり、外部Web検索の対象ではありません。"
        "目の前の相手との会話文脈に基づいて応答してください。"
    )


class _CommandError(Exception):
    """コマンド単位でモデルへ返すエラー（他コマンドは継続する）"""


class _DeadlineExceeded(Exception):
    """1回の呼び出し全体の締め切り超過"""


@dataclass
class _SearchCommand:
    q: str
    recency: int | None = None
    domains: list[str] = field(default_factory=list)


@dataclass
class _OpenCommand:
    ref_id: str
    lineno: int | None = None


@dataclass
class _FindCommand:
    ref_id: str
    pattern: str


@dataclass
class _RunContext:
    registry: "PageRegistry"
    scope: str
    dispatch_context: str
    profile: _LengthProfile
    is_fact_check: bool | None
    target_name: str
    other_names: set[str]
    deadline: float
    errors: int = 0


# ---------------------------------------------------------------------------
# 入力正規化ヘルパー（LLM 出力の揺らぎ吸収）
# ---------------------------------------------------------------------------


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("[", "{"):
            try:
                return json.loads(stripped)
            except Exception:
                return value
    return value


def _as_list(value: Any) -> list[Any]:
    value = _maybe_json(value)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_str(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = item.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _normalize_domain(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urlparse(text).hostname or ""
    text = text.split("/", 1)[0].lstrip("*.").strip(".")
    return text if _DOMAIN_RE.fullmatch(text) else ""


def _parse_domains(raw: Any) -> list[str]:
    raw = _maybe_json(raw)
    if isinstance(raw, str):
        items: list[Any] = [p for p in re.split(r"[,\s]+", raw) if p]
    else:
        items = _as_list(raw)
    domains: list[str] = []
    for item in items:
        dom = _normalize_domain(item)
        if dom and dom not in domains:
            domains.append(dom)
    return domains[:MAX_DOMAINS_PER_QUERY]


def _parse_search_commands(raw: Any) -> list[_SearchCommand]:
    commands: list[_SearchCommand] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in _as_list(raw):
        if isinstance(item, dict):
            q = _first_str(item, ("q", "query", "search_query", "keyword"))
            cmd = _SearchCommand(q=q, recency=_to_int(item.get("recency")), domains=_parse_domains(item.get("domains")))
        elif item is not None and str(item).strip():
            cmd = _SearchCommand(q=str(item).strip())
        else:
            continue
        if not cmd.q:
            continue
        key = (cmd.q, tuple(cmd.domains))
        if key in seen:
            continue
        seen.add(key)
        commands.append(cmd)
    return commands


def _parse_open_commands(raw: Any) -> list[_OpenCommand]:
    commands: list[_OpenCommand] = []
    for item in _as_list(raw):
        if isinstance(item, dict):
            ref = _first_str(item, ("ref_id", "id", "url", "ref"))
            lineno = _to_int(item.get("lineno", item.get("line", item.get("line_no"))))
        elif item is not None and str(item).strip():
            ref, lineno = str(item).strip(), None
        else:
            continue
        commands.append(_OpenCommand(ref_id=ref, lineno=lineno))
    return commands


def _parse_find_commands(raw: Any) -> list[_FindCommand]:
    commands: list[_FindCommand] = []
    for item in _as_list(raw):
        if not isinstance(item, dict):
            continue
        ref = _first_str(item, ("ref_id", "id", "url", "ref"))
        pattern = str(item.get("pattern") or item.get("query") or item.get("q") or item.get("text") or "").strip()
        commands.append(_FindCommand(ref_id=ref, pattern=pattern))
    return commands


def _resolve_scope(scope: Any, context: Any, kwargs: dict[str, Any]) -> str:
    if scope is not None and str(scope).strip():
        return str(scope).strip()
    for src in (context if isinstance(context, dict) else {}, kwargs):
        for key in _SCOPE_KEYS:
            val = src.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
    return "default"


def _resolve_dispatch_context(context: Any) -> str:
    if isinstance(context, str):
        return context
    if isinstance(context, dict):
        return str(context.get("conversation_text") or context.get("text") or "")
    return ""


def _resolve_profile(response_length: Any) -> _LengthProfile:
    key = str(response_length or DEFAULT_RESPONSE_LENGTH).strip().lower()
    profile = _LENGTH_PROFILES.get(key)
    if profile is None:
        log.info("[web-run] Unknown response_length=%r; falling back to %s", response_length, DEFAULT_RESPONSE_LENGTH)
        profile = _LENGTH_PROFILES[DEFAULT_RESPONSE_LENGTH]
    return profile


# ---------------------------------------------------------------------------
# 出力整形ヘルパー
# ---------------------------------------------------------------------------


def _compact(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _sources_of(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    return [s for s in (result.get("sources") or []) if isinstance(s, dict)]


def _source_url(source: dict[str, Any]) -> str:
    return str(source.get("url") or source.get("href") or "").strip()


def _host_matches(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def _apply_domains(query: str, domains: list[str]) -> str:
    if not domains:
        return query
    if len(domains) == 1:
        return f"{query} site:{domains[0]}"
    return f"{query} (" + " OR ".join(f"site:{d}" for d in domains) + ")"


def _truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """全体上限を超えたら末尾を行境界で切り、続きの取り方を注記する（注記込みで limit 以内）。"""
    if len(text) <= limit:
        return text, False
    note = f"\n…（出力上限（約{limit:,}字）に達したため以降を省略しました。続きは open で lineno を指定してください）"
    budget = max(limit - len(note), 0)
    cut = text[:budget]
    newline_pos = cut.rfind("\n")
    if newline_pos >= budget // 2:
        cut = cut[:newline_pos]
    return cut.rstrip() + note, True


def _error_text(err: BaseException) -> str:
    if isinstance(err, _DeadlineExceeded):
        return _TIMEOUT_MESSAGE
    msg = _compact(str(err)) or type(err).__name__
    return _clip(msg, 200)


def _get_page_registry() -> "PageRegistry":
    """呼び出し時点の default_registry を返す（テストでの差し替えに追従するため遅延参照）。"""
    from ..web_research import page_registry as page_registry_module

    return page_registry_module.default_registry


class WebRunToolAdapter:
    """web.run 型バッチリサーチツール（web_run）のアダプター"""

    @classmethod
    def create_tool(cls, name: str = TOOL_NAME, timeout_sec: float = DEFAULT_TOOL_TIMEOUT_SEC) -> ToolDefinition:
        """web_run ツール定義を作成する（デフォルトタイムアウト: 240秒）"""
        description = (
            "Web調査（検索・ページを開く・ページ内検索）を1回の呼び出しに束ねて実行します。"
            "最新ニュース、事実確認、未知語の意味、数値や設定に基づく影響・結果のシミュレーション検証など客観的調査が必要な場合に使用してください。\n"
            "使い方: search_query=[{\"q\": \"検索語\", \"domains\": [\"example.com\"]}]（最大4件）で検索すると各結果に参照ID s0, s1… が付きます。"
            "open=[{\"ref_id\": \"s0\"}] で検索結果のページ本文を行番号つきで開き（開いたページには p0, p1… が付きます）、"
            "{\"ref_id\": \"p0\", \"lineno\": 120} で120行目付近を、{\"ref_id\": \"https://...\"} で URL を直接開きます。"
            "find=[{\"ref_id\": \"p0\", \"pattern\": \"探す語\"}] でページ内の該当行を探します。"
            "response_length は short（抜粋のみ）/ medium（既定）/ long。recency（日数）は現状ヒント扱いで期間絞り込みは保証されません。\n"
            "作法: (1) ref_id（s0 / p0 等）は内部参照なので最終回答には出さず、出典はタイトルと URL で示すこと。"
            "(2) 抜粋で足りなければ推測で補わず open でページを開き、ページ内の探し物は find を使うこと。"
            "(3) 1回の呼び出しに複数のコマンドを束ねてよい（往復を減らす）。"
        )
        return ToolDefinition(
            name=name,
            description=description,
            parameters=[
                ToolParameter(
                    name="search_query",
                    type="array",
                    description="検索コマンドのリスト（最大4件）。各要素は {\"q\": 検索語, \"recency\": 日数(任意), \"domains\": [ドメイン](任意)}",
                    required=False,
                    items={
                        "type": "object",
                        "properties": {
                            "q": {"type": "string", "description": "検索語"},
                            "recency": {"type": "integer", "description": "直近何日の情報を優先するか（ヒント扱い）"},
                            "domains": {"type": "array", "items": {"type": "string"}, "description": "対象ドメイン（例: example.com）"},
                        },
                        "required": ["q"],
                    },
                ),
                ToolParameter(
                    name="open",
                    type="array",
                    description="開くページのリスト。各要素は {\"ref_id\": s0 / p0 / URL, \"lineno\": 中心にする行番号(任意)}",
                    required=False,
                    items={
                        "type": "object",
                        "properties": {
                            "ref_id": {"type": "string", "description": "検索結果ID（s0）、ページID（p0）、または URL"},
                            "lineno": {"type": "integer", "description": "この行を中心に表示する（任意）"},
                        },
                        "required": ["ref_id"],
                    },
                ),
                ToolParameter(
                    name="find",
                    type="array",
                    description="ページ内検索のリスト。各要素は {\"ref_id\": p0 / s0 / URL, \"pattern\": 探す語}",
                    required=False,
                    items={
                        "type": "object",
                        "properties": {
                            "ref_id": {"type": "string", "description": "ページID（p0）、検索結果ID（s0）、または URL"},
                            "pattern": {"type": "string", "description": "ページ内で探す語"},
                        },
                        "required": ["ref_id", "pattern"],
                    },
                ),
                ToolParameter(
                    name="response_length",
                    type="string",
                    description="出力量: short（抜粋のみ・約2,000字）/ medium（既定・ページ120行・約8,000字）/ long（ページ300行・約20,000字）",
                    required=False,
                ),
            ],
            handler=cls.execute,
            category=ToolCategory.WEB,
            tags={"web", "search", "research", "browse"},
            timeout_sec=timeout_sec,
        )

    # ------------------------------------------------------------------
    # エントリポイント
    # ------------------------------------------------------------------
    @classmethod
    async def execute(
        cls,
        search_query: Any = None,
        open: Any = None,  # noqa: A002 - web.run プロトコルのコマンド名に合わせる
        find: Any = None,
        response_length: str | None = DEFAULT_RESPONSE_LENGTH,
        *,
        scope: str | None = None,
        is_fact_check: bool | None = None,
        context: dict[str, Any] | Any = None,
        target_name: str | None = None,
        other_names: list[str] | set[str] | str | None = None,
        time_budget_sec: float | None = None,
        **kwargs: Any,
    ) -> str:
        """コマンドの束を search → open → find の順に実行し、Observation 向けテキストを返す。"""
        if not search_query:
            for key in _SEARCH_FALLBACK_KEYS:
                if kwargs.get(key):
                    search_query = kwargs[key]
                    break

        searches = _parse_search_commands(search_query)
        opens = _parse_open_commands(open)
        finds = _parse_find_commands(find)

        if len(searches) > MAX_SEARCH_QUERIES:
            log.warning(
                "[web-run] search_query truncated: %d -> %d dropped=%r",
                len(searches),
                MAX_SEARCH_QUERIES,
                [c.q for c in searches[MAX_SEARCH_QUERIES:]],
            )
            searches = searches[:MAX_SEARCH_QUERIES]
        if len(opens) > MAX_OPEN_COMMANDS:
            log.warning("[web-run] open truncated: %d -> %d", len(opens), MAX_OPEN_COMMANDS)
            opens = opens[:MAX_OPEN_COMMANDS]
        if len(finds) > MAX_FIND_COMMANDS:
            log.warning("[web-run] find truncated: %d -> %d", len(finds), MAX_FIND_COMMANDS)
            finds = finds[:MAX_FIND_COMMANDS]

        profile = _resolve_profile(response_length)
        resolved_scope = _resolve_scope(scope, context, kwargs)

        if not (searches or opens or finds):
            return "web_run: search_query / open / find のいずれかを指定してください。"

        resolved_target_name, resolved_other_names = extract_partner_names(
            context=context,
            target_name=target_name,
            other_names=other_names,
            **{k: v for k, v in kwargs.items() if k != "context"},
        )

        loop = asyncio.get_running_loop()
        budget = float(time_budget_sec) if time_budget_sec else DEFAULT_TOOL_TIMEOUT_SEC - _TIMEOUT_MARGIN_SEC
        ctx = _RunContext(
            registry=_get_page_registry(),
            scope=resolved_scope,
            dispatch_context=_resolve_dispatch_context(context),
            profile=profile,
            is_fact_check=is_fact_check,
            target_name=resolved_target_name,
            other_names=resolved_other_names,
            deadline=loop.time() + max(budget, 1.0),
        )

        sections: list[str] = []
        for search_cmd in searches:
            sections.append(await cls._safe_section(ctx, cls._run_search(ctx, search_cmd), f"検索: 「{search_cmd.q}」\n- エラー: "))
        for open_cmd in opens:
            sections.append(await cls._safe_section(ctx, cls._run_open(ctx, open_cmd), f"ページ [{open_cmd.ref_id}] 取得エラー: "))
        for find_cmd in finds:
            sections.append(
                await cls._safe_section(
                    ctx, cls._run_find(ctx, find_cmd), f"ページ内検索 [{find_cmd.ref_id}] 「{find_cmd.pattern}」: エラー: "
                )
            )

        output, truncated = _truncate_output("\n\n".join(s for s in sections if s), profile.total_chars)
        log.info(
            "[web-run] Executed searches=%d opens=%d finds=%d errors=%d response_length=%s result_chars=%d truncated=%s scope=%s",
            len(searches),
            len(opens),
            len(finds),
            ctx.errors,
            profile.name,
            len(output),
            truncated,
            resolved_scope,
        )
        return output

    # 互換エイリアス（WebSearchToolAdapter と同様のインターフェース）
    execute_search = execute

    # ------------------------------------------------------------------
    # 共通: 締め切り・例外の封じ込め
    # ------------------------------------------------------------------
    @classmethod
    async def _safe_section(cls, ctx: _RunContext, coro: Awaitable[str], error_prefix: str) -> str:
        """1コマンドの失敗で全体を落とさないための最終防衛ライン。"""
        try:
            return await coro
        except Exception as e:  # _CommandError / _DeadlineExceeded / 想定外例外を一括封じ込め
            ctx.errors += 1
            if not isinstance(e, (_CommandError, _DeadlineExceeded)):
                log.warning("[web-run] Command failed unexpectedly: %s", e, exc_info=True)
            return f"{error_prefix}{_error_text(e)}"

    @staticmethod
    async def _with_deadline(ctx: _RunContext, factory: Callable[[], Awaitable[Any]]) -> Any:
        remaining = ctx.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise _DeadlineExceeded()
        try:
            return await asyncio.wait_for(factory(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise _DeadlineExceeded() from e

    # ------------------------------------------------------------------
    # search_query
    # ------------------------------------------------------------------
    @classmethod
    async def _dispatch_search(cls, ctx: _RunContext, query: str) -> tuple[dict[str, Any], str, bool]:
        """browser_search を優先し、失敗・結果なしなら simple_search へフォールバックする（search_web と同じ多重防御）。"""
        from harness.agent.tool_registry.web_research.dispatch import research_dispatch

        dispatch_kwargs: dict[str, Any] = {"context": ctx.dispatch_context}
        if ctx.is_fact_check is not None:
            dispatch_kwargs["is_fact_check"] = ctx.is_fact_check

        last_result: dict[str, Any] | None = None
        last_mode = "browser_search"
        last_error: Exception | None = None
        for idx, mode in enumerate(("browser_search", "simple_search")):

            def _factory(m: str = mode) -> Awaitable[Any]:
                return research_dispatch(user_text=query, queries=[query], mode=m, **dispatch_kwargs)  # type: ignore[arg-type]

            try:
                res = await cls._with_deadline(ctx, _factory)
            except _DeadlineExceeded:
                if last_result is None and last_error is None:
                    raise
                break
            except Exception as e:
                last_error = e
                log.warning("[web-run] %s dispatch exception query=%r: %s", mode, query, e)
                continue
            if isinstance(res, dict):
                last_result, last_mode = res, mode
                if any(_source_url(s) for s in _sources_of(res)):
                    return res, mode, idx > 0
        if last_result is not None:
            return last_result, last_mode, last_mode != "browser_search"
        raise last_error or _CommandError("検索結果を取得できませんでした。")

    @classmethod
    async def _run_search(cls, ctx: _RunContext, cmd: _SearchCommand) -> str:
        header = f"検索: 「{cmd.q}」"

        # 対話相手名（target_name）誤Web検索ガード (User-First Resolution) — search_web と同一判定・同一文面
        blocked, matched_name = is_partner_name_query(cmd.q, target_name=ctx.target_name, other_names=ctx.other_names)
        if blocked:
            guidance = build_partner_guard_message(matched_name)
            log.info("[web-run] Partner name search blocked: query=%r target_name=%r", cmd.q, matched_name)
            return f"{header}\n{guidance}"

        if cmd.recency is not None:
            log.info("[web-run] recency=%d is advisory (not enforced by search backend) query=%r", cmd.recency, cmd.q)

        effective_query = _apply_domains(cmd.q, cmd.domains)
        result, mode, fallback_used = await cls._dispatch_search(ctx, effective_query)

        profile = ctx.profile
        seen_urls: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for source in _sources_of(result):
            url = _source_url(source)
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            candidates.append(source)
        domain_filtered_out = False
        if cmd.domains:
            matched = [s for s in candidates if _host_matches(_source_url(s), cmd.domains)]
            domain_filtered_out = bool(candidates) and not matched
            candidates = matched
        hits = candidates[: profile.hits_per_query]

        ref_ids = ctx.registry.register_search_results(
            ctx.scope,
            [
                {
                    "url": _source_url(s),
                    "title": str(s.get("title") or "").strip(),
                    "snippet": str(s.get("content_excerpt") or s.get("snippet") or s.get("body") or "").strip(),
                    # browser_search がすでにブラウザで読んだ本文。open 時の再取得を省くために保持する
                    "content_text": str(s.get("content_text") or ""),
                }
                for s in hits
            ],
        )

        lines = [header]
        summary = _compact(result.get("summary"))
        # LLM 要約が行われた経路（plan 付き）のみ概要を載せる。それ以外の summary は出典一覧の重複になる
        if summary and isinstance(result.get("plan"), dict):
            lines.append(f"概要: {_clip(summary, profile.summary_chars)}")
        for ref_id, source in zip(ref_ids, hits):
            title = _compact(source.get("title")) or "Web情報"
            snippet = _compact(source.get("content_excerpt") or source.get("snippet") or source.get("body") or "")
            snippet_part = f": {_clip(snippet, profile.snippet_chars)}" if snippet else ""
            lines.append(f"- [{ref_id}] {title}{snippet_part} ({_source_url(source)})")
        if not hits:
            error = _compact(result.get("error"))
            if domain_filtered_out:
                lines.append(f"- 指定ドメイン（{', '.join(cmd.domains)}）に一致する結果はありませんでした")
            elif error:
                ctx.errors += 1
                lines.append(f"- エラー: {_clip(error, 200)}")
            else:
                lines.append("- 該当する結果はありませんでした")

        log.info(
            "[web-run] search query=%r mode=%s fallback_used=%s hits=%d scope=%s",
            effective_query,
            mode,
            fallback_used,
            len(hits),
            ctx.scope,
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # open / find 共通: ページの確保
    # ------------------------------------------------------------------
    @classmethod
    async def _fetch_and_register(cls, ctx: _RunContext, url: str, title_hint: str = "") -> tuple[str, dict[str, Any]]:
        from harness.agent.tool_registry.web_research.dispatch import research_dispatch

        result = await cls._with_deadline(
            ctx, lambda: research_dispatch(user_text=url, context="", mode="browser_read_url")
        )
        if not isinstance(result, dict):
            raise _CommandError("ページを取得できませんでした。")

        sources = _sources_of(result)
        body_source = next((s for s in sources if str(s.get("content_text") or "").strip()), None)
        if body_source is not None:
            text = str(body_source.get("content_text") or "")
            title = str(body_source.get("title") or "").strip() or title_hint
        else:
            # ブラウザ本文が取れず、リンク要約（build_link_summaries_from_text）のみ得られた場合
            summaries = [
                str(s.get("snippet") or "").strip()
                for s in sources
                if s.get("source_type") == "link_summary" and str(s.get("snippet") or "").strip()
            ]
            if not summaries:
                error = _compact(result.get("error"))
                raise _CommandError(error or "ページ本文を取得できませんでした。")
            text = "\n".join(summaries)
            title = f"{title_hint or 'Webページ'}（本文取得不可のため要約）"

        ref_id = ctx.registry.register_page(ctx.scope, url, title, text)
        page = ctx.registry.resolve(ctx.scope, ref_id)
        if page is None:
            raise _CommandError("ページの保持に失敗しました。")
        log.info("[web-run] page fetched ref=%s url=%s lines=%d scope=%s", ref_id, url, len(page.get("lines") or []), ctx.scope)
        return ref_id, page

    @classmethod
    async def _ensure_page(cls, ctx: _RunContext, ref: str) -> tuple[str, dict[str, Any], bool]:
        """ref（pN / sN / URL）からページを確保し、(ページref_id, ページ情報, 今回新規に取得したか) を返す。"""
        ref = str(ref or "").strip()
        if not ref:
            raise _CommandError("ref_id が指定されていません。")

        if _PAGE_REF_RE.fullmatch(ref):
            page = ctx.registry.resolve(ctx.scope, ref)
            if page is None or page.get("kind") != "page":
                raise _CommandError(
                    f"[{ref}] は見つかりません（未取得か、30分以上経過または上限超過で破棄された可能性があります）。URL を指定して開き直してください。"
                )
            return ref, page, False

        if _SEARCH_REF_RE.fullmatch(ref):
            hit = ctx.registry.resolve(ctx.scope, ref)
            if hit is None or hit.get("kind") != "search_hit":
                raise _CommandError(
                    f"[{ref}] は見つかりません（30分以上経過または上限超過で破棄された可能性があります）。search_query で検索し直してください。"
                )
            url = str(hit.get("url") or "").strip()
            if not url:
                raise _CommandError(f"[{ref}] には URL がありません。")
            existing = ctx.registry.find_page_by_url(ctx.scope, url)
            if existing:
                page = ctx.registry.resolve(ctx.scope, existing)
                if page is not None:
                    return existing, page, False
            cached = str(hit.get("content_text") or "")
            if cached.strip():
                new_ref = ctx.registry.register_page(ctx.scope, url, str(hit.get("title") or ""), cached)
                page = ctx.registry.resolve(ctx.scope, new_ref)
                if page is not None:
                    log.info("[web-run] page registered from search cache ref=%s url=%s scope=%s", new_ref, url, ctx.scope)
                    return new_ref, page, True
            new_ref, page = await cls._fetch_and_register(ctx, url, title_hint=str(hit.get("title") or ""))
            return new_ref, page, True

        if _URL_RE.fullmatch(ref):
            existing = ctx.registry.find_page_by_url(ctx.scope, ref)
            if existing:
                page = ctx.registry.resolve(ctx.scope, existing)
                if page is not None:
                    return existing, page, False
            new_ref, page = await cls._fetch_and_register(ctx, ref)
            return new_ref, page, True

        raise _CommandError(f"ref_id「{ref}」は不正です。s0 / p0 形式の参照IDか、http(s) の URL を指定してください。")

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------
    @staticmethod
    def _render_page(ref_id: str, page: dict[str, Any], lineno: int | None, profile: _LengthProfile) -> str:
        lines: list[str] = list(page.get("lines") or [])
        total = len(lines)
        title = _compact(page.get("title")) or "(タイトルなし)"
        header = f"ページ [{ref_id}] {title} ({page.get('url') or ''}) 全{total}行"
        if total == 0:
            return f"{header}\n（本文テキストを抽出できませんでした）"

        size = profile.page_lines
        out = [header]
        if lineno is None:
            start = 1
        else:
            target = min(max(lineno, 1), total)
            if target != lineno:
                out.append(f"（指定行 {lineno} は範囲外のため {target} 行目付近を表示）")
            start = max(1, target - size // 2)
        end = min(total, start + size - 1)
        start = max(1, min(start, end - size + 1))
        out.extend(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        if start > 1 or end < total:
            out.append(f"（全{total}行中 {start}〜{end}行目を表示。続きは open で lineno を指定）")
        if page.get("truncated"):
            out.append("（本文が長いため先頭60,000字までを保持しています）")
        return "\n".join(out)

    @classmethod
    async def _run_open(cls, ctx: _RunContext, cmd: _OpenCommand) -> str:
        ref_id, page, fetched = await cls._ensure_page(ctx, cmd.ref_id)
        log.info("[web-run] open ref=%r -> %s fetched=%s lineno=%s scope=%s", cmd.ref_id, ref_id, fetched, cmd.lineno, ctx.scope)
        return cls._render_page(ref_id, page, cmd.lineno, ctx.profile)

    # ------------------------------------------------------------------
    # find
    # ------------------------------------------------------------------
    @classmethod
    async def _run_find(cls, ctx: _RunContext, cmd: _FindCommand) -> str:
        if not cmd.pattern:
            raise _CommandError("pattern が指定されていません。")
        ref_id, page, fetched = await cls._ensure_page(ctx, cmd.ref_id)
        matches = ctx.registry.find_in_page(ctx.scope, ref_id, cmd.pattern)
        out = [f"ページ内検索 [{ref_id}] 「{cmd.pattern}」: {len(matches)}件"]
        if fetched or ref_id != cmd.ref_id:
            out.append(f"（{cmd.ref_id} を [{ref_id}] {_compact(page.get('title')) or '(タイトルなし)'} ({page.get('url') or ''}) として取得して検索）")
        out.extend(f"{m['lineno']}: {m['line']}" for m in matches)
        if len(matches) >= ctx.registry.max_find_results:
            out.append(f"（最大{ctx.registry.max_find_results}件まで表示）")
        log.info("[web-run] find ref=%s pattern=%r matches=%d fetched=%s scope=%s", ref_id, cmd.pattern, len(matches), fetched, ctx.scope)
        return "\n".join(out)


__all__ = ["WebRunToolAdapter", "build_partner_guard_message", "TOOL_NAME"]
