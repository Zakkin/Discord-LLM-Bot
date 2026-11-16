"""Discord専用: 後日調査フォローアップ（登録・裏調査・メンション使い分け配達）。

利用者が頼んだ調べ物やその場で調べ切れなかった話題を裏で引き受け、
既存Web検索（search_web_entries_unified）と中間モデル要約で調査し、
最低3時間あけて適切なタイミングで配達する（§62）。
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import inspect
import json
import logging
import re
import unicodedata
from typing import Any, Iterable

try:
    from lib.error_report import report_error
except ImportError:
    def report_error(site: str, exc: BaseException) -> None:
        pass

try:
    from ollama_bot.common.config_helpers import (
        cfg,
        cfg_bool,
        cfg_int,
        cfg_persona_namespace,
    )
except ImportError:
    def cfg(name: str, default: Any = None) -> Any:
        return default

    def cfg_bool(name: str, default: bool = False) -> bool:
        return bool(default)

    def cfg_int(name: str, default: int = 0) -> int:
        return int(default)

    def cfg_persona_namespace(*args: Any, **kwargs: Any) -> str:
        return "default"

try:
    from harness.agent.tool_registry.web_research import search_web_entries_unified
except ImportError:
    async def search_web_entries_unified(query: str, *, max_results: int = 4) -> list[dict[str, str]]:
        return []

try:
    from ollama_bot.common.ollama_helpers import (
        call_ollama,
        call_ollama_json,
        sanitize_generated_reply,
    )
except ImportError:
    async def call_ollama_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def call_ollama(*args: Any, **kwargs: Any) -> str:
        return ""

    def sanitize_generated_reply(text: str, *args: Any, **kwargs: Any) -> str:
        return str(text or "").strip()

log = logging.getLogger("front.discord.research_followup")

_JST = timezone(timedelta(hours=9))

# 既定値（cfgのデフォルト引数として使用）
DEFAULT_RESEARCH_FOLLOWUP_ENABLED = True
DEFAULT_RESEARCH_FOLLOWUP_MIN_DELAY_HOURS = 3
DEFAULT_RESEARCH_FOLLOWUP_MAX_PER_USER = 5
DEFAULT_RESEARCH_FOLLOWUP_MAX_DELIVERIES_PER_DAY = 2
DEFAULT_RESEARCH_FOLLOWUP_ACTIVE_MINUTES = 30
DEFAULT_RESEARCH_FOLLOWUP_LOOP_MINUTES = 30
DEFAULT_RESEARCH_FOLLOWUP_EXPIRE_DAYS = 7

_EXPLICIT_RESEARCH_RE = re.compile(
    r"(調べておいて|調べといて|調べてほしい|調べてくれ|後で教え|あとで教え|今度教え|調べて)"
)

_REQUEST_CLEAN_SUFFIX_RE = re.compile(
    r"(?:について|に関して|のこと(?:を|について)?|とか|って|を|は|が|も)?\s*"
    r"(?:ちょっと|少し|詳しく|あとで|後で|今度|裏で|暇な時に|時間ある時に)?\s*"
    r"(?:調べておいて|調べといて|調べてほしい|調べてくれ|後で教えて|あとで教えて|今度教えて|後で教え|あとで教え|今度教え|調べて)"
    r"(?:ください|下さい|もらえる|くれる|ね|よ|な|ー|〜|！|!|？|\?|。|、|\s)*$"
)
_REQUEST_CLEAN_PREFIX_RE = re.compile(
    r"^(?:あとで|後で|今度|ちょっと|少し|ねえ|あのさ|そういえば)\s*[、，\s]*"
)

_RESEARCH_MEMO_PROMPT_INSTRUCTION = (
    "検索結果の事実だけを 2〜4 文にまとめる。出典に無いことを足さない。分からない点は分からないと書く"
)
_RESEARCH_MEMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {"type": "string"},
    },
    "required": ["findings"],
}

_DELIVERY_PROMPT_INSTRUCTION = (
    "以前頼まれていた調べ物の結果を、思い出したように自然に伝える。2〜4 文。"
    "出典リンクは 1〜2 件まで本文に含める。『タスク』『メモ』等の内部表現は出さない"
)


def _to_jst(dt: datetime) -> datetime:
    """datetime を JST タイムゾーン付きに正規化する（naive は JST とみなす）。"""
    if dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None:
        return dt.astimezone(_JST)
    return dt.replace(tzinfo=_JST)


def _parse_iso_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _to_jst(value)
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return _to_jst(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _is_quiet_hours_jst(now: datetime) -> bool:
    """静かな時間帯（23:00〜07:59 JST）かどうかを判定する。"""
    jst_now = _to_jst(now)
    return jst_now.hour >= 23 or jst_now.hour < 8


def is_explicit_research_request(text: str) -> bool:
    """発言に明示的な後日調査依頼が含まれるかを判定する。"""
    if not text:
        return False
    return bool(_EXPLICIT_RESEARCH_RE.search(str(text)))


def extract_research_query(text: str) -> str:
    """対象発言から依頼表現を除いた話題の核を取り出す（取れなければ発言全体）。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"<@!?\d+>", "", raw).strip()
    cleaned = _REQUEST_CLEAN_PREFIX_RE.sub("", cleaned).strip()
    cleaned = _REQUEST_CLEAN_SUFFIX_RE.sub("", cleaned).strip()
    if cleaned == raw or _EXPLICIT_RESEARCH_RE.search(cleaned):
        cleaned = _EXPLICIT_RESEARCH_RE.sub("", cleaned).strip()
        cleaned = re.sub(r"(?:について|に関して|のこと|を|って|は|が)\s*$", "", cleaned).strip()
    cleaned = cleaned.strip(" 　、。！？!?「」『』・")
    return cleaned or raw


def decide_mention(
    *,
    explicit: bool,
    now: datetime,
    user_last_active_at: datetime | None,
    mentions_today: int,
) -> bool:
    """メンション判断（決定的）。規則:
    - 静かな時間帯（23:00〜07:59 JST）→ 常に False
    - その日のメンション配達が既に 1 件ある → False（メンションは 1 日 1 件まで）
    - explicit かつ利用者の最終発言が ACTIVE_MINUTES 以内 → True
    - それ以外 → False
    """
    if _is_quiet_hours_jst(now):
        return False
    if int(mentions_today) >= 1:
        return False
    if not explicit or user_last_active_at is None:
        return False

    active_minutes = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_ACTIVE_MINUTES", DEFAULT_RESEARCH_FOLLOWUP_ACTIVE_MINUTES) or DEFAULT_RESEARCH_FOLLOWUP_ACTIVE_MINUTES),
    )
    elapsed_sec = (_to_jst(now) - _to_jst(user_last_active_at)).total_seconds()
    return elapsed_sec <= active_minutes * 60


def should_deliver_now(
    *,
    explicit: bool,
    now: datetime,
    deliver_after: datetime,
    user_last_active_at: datetime | None,
    channel_last_active_at: datetime | None,
) -> bool:
    """配達タイミングの判断（決定的）。
    - deliver_after 未到来 → False
    - explicit かつ利用者が ACTIVE_MINUTES 以内に発言 → True（メンション配達の好機）
    - explicit で利用者が長く不在でも、deliver_after から 12 時間超過 → True（メンションなしで置く）
    - 非 explicit は、チャンネルの最終活動が 60 分以内 → True
    - 静かな時間帯は、チャンネルが 15 分以内に活動している場合のみ True
    """
    now_jst = _to_jst(now)
    deliver_after_jst = _to_jst(deliver_after)
    if now_jst < deliver_after_jst:
        return False

    if _is_quiet_hours_jst(now_jst):
        if channel_last_active_at is None:
            return False
        ch_elapsed = (now_jst - _to_jst(channel_last_active_at)).total_seconds()
        return ch_elapsed <= 15 * 60

    active_minutes = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_ACTIVE_MINUTES", DEFAULT_RESEARCH_FOLLOWUP_ACTIVE_MINUTES) or DEFAULT_RESEARCH_FOLLOWUP_ACTIVE_MINUTES),
    )
    if explicit:
        if user_last_active_at is not None:
            user_elapsed = (now_jst - _to_jst(user_last_active_at)).total_seconds()
            if user_elapsed <= active_minutes * 60:
                return True
        since_deliver_after = (now_jst - deliver_after_jst).total_seconds()
        if since_deliver_after >= 12 * 3600:
            return True
        return False

    if channel_last_active_at is not None:
        ch_elapsed = (now_jst - _to_jst(channel_last_active_at)).total_seconds()
        if ch_elapsed <= 60 * 60:
            return True
    return False


# ---------------------------------------------------------------------------
# 保存層（§61）呼び出しラッパー（store メソッド / モック差し替え両対応）
# ---------------------------------------------------------------------------


async def _call_store_method(store: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    if store is None:
        return None
    fn = getattr(store, method_name, None)
    if not callable(fn):
        return None
    res = fn(*args, **kwargs)
    if inspect.isawaitable(res):
        return await res
    return res


async def list_user_active_research_followups(
    store: Any,
    *,
    persona: str,
    target_user_id: str | int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    res = await _call_store_method(
        store,
        "list_user_active_research_followups",
        persona=persona,
        target_user_id=target_user_id,
        limit=limit,
    )
    return list(res) if isinstance(res, list) else []


async def add_research_followup(
    store: Any,
    *,
    persona: str,
    guild_id: str | int | None = None,
    channel_id: str | int,
    target_user_id: str | int,
    request_text: str,
    query: str,
    explicit: bool | int = False,
    now_iso: str | None = None,
) -> int | None:
    kwargs: dict[str, Any] = {
        "persona": persona,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "target_user_id": target_user_id,
        "request_text": request_text,
        "query": query,
        "explicit": 1 if bool(explicit) else 0,
    }
    if now_iso is not None:
        kwargs["now_iso"] = now_iso
    res = await _call_store_method(store, "add_research_followup", **kwargs)
    return int(res) if isinstance(res, int) else None


async def list_pending_research_followups(
    store: Any,
    *,
    persona: str,
    limit: int = 2,
) -> list[dict[str, Any]]:
    res = await _call_store_method(
        store,
        "list_pending_research_followups",
        persona=persona,
        limit=limit,
    )
    return list(res) if isinstance(res, list) else []


async def mark_research_followup_researched(
    store: Any,
    record_id: int,
    *,
    findings: str,
    sources_json: str | list[Any] | None,
    deliver_after: str,
    now_iso: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "findings": findings,
        "sources_json": sources_json,
        "deliver_after": deliver_after,
    }
    if now_iso is not None:
        kwargs["now_iso"] = now_iso
    await _call_store_method(
        store,
        "mark_research_followup_researched",
        record_id,
        **kwargs,
    )


async def increment_research_followup_attempts(store: Any, record_id: int) -> int:
    res = await _call_store_method(store, "increment_research_followup_attempts", record_id)
    return int(res) if isinstance(res, int) else 0


async def mark_research_followup_failed(store: Any, record_id: int) -> None:
    await _call_store_method(store, "mark_research_followup_failed", record_id)


async def list_deliverable_research_followups(
    store: Any,
    *,
    persona: str,
    now_iso: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    res = await _call_store_method(
        store,
        "list_deliverable_research_followups",
        persona=persona,
        now_iso=now_iso,
        limit=limit,
    )
    return list(res) if isinstance(res, list) else []


async def mark_research_followup_delivered(
    store: Any,
    record_id: int,
    *,
    mention: bool | int,
    now_iso: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {"mention": bool(mention)}
    if now_iso is not None:
        kwargs["now_iso"] = now_iso
    await _call_store_method(
        store,
        "mark_research_followup_delivered",
        record_id,
        **kwargs,
    )


async def expire_research_followups(
    store: Any,
    *,
    persona: str,
    cutoff_iso: str,
) -> int:
    res = await _call_store_method(
        store,
        "expire_research_followups",
        persona=persona,
        cutoff_iso=cutoff_iso,
    )
    return int(res) if isinstance(res, int) else 0


# ---------------------------------------------------------------------------
# Step 2: 登録
# ---------------------------------------------------------------------------


def _extract_query_terms(text: str) -> set[str]:
    norm = unicodedata.normalize("NFKC", str(text or "")).lower().strip()
    norm = extract_research_query(norm)
    if not norm:
        return set()
    parts = re.split(r"[\s、。！？!?,.・/\\「」『』（）()\[\]【】]+|(?:について|に関して|とは|って|の|を|が|は|に|で|と)", norm)
    terms = {p.strip() for p in parts if len(p.strip()) >= 2}
    if not terms and norm:
        terms.add(norm)
    return terms


def _is_duplicate_query(new_query: str, existing_query: str) -> bool:
    q1 = unicodedata.normalize("NFKC", extract_research_query(new_query)).lower().strip()
    q2 = unicodedata.normalize("NFKC", extract_research_query(existing_query)).lower().strip()
    if not q1 or not q2:
        return False
    if q1 == q2 or q1 in q2 or q2 in q1:
        return True
    t1 = _extract_query_terms(q1)
    t2 = _extract_query_terms(q2)
    if not t1 or not t2:
        return False
    overlap = len(t1 & t2) / min(len(t1), len(t2))
    return overlap >= 0.5


def _did_research_attach(research_result: dict[str, Any] | None) -> bool:
    if not isinstance(research_result, dict) or not research_result:
        return False
    if str(research_result.get("error") or "").strip():
        return False
    if bool(research_result.get("insufficient")):
        return False
    summary = str(research_result.get("summary") or "").strip()
    sources = list(research_result.get("sources") or [])
    if not summary and not sources:
        return False
    if summary in {
        "検索結果が見つかりませんでした。",
        "URLの読み取りに失敗しました。",
        "検索クエリを作れませんでした。",
    }:
        return False
    if not sources and summary.startswith("検索がタイムアウトしました。"):
        return False
    return True


def _is_implicit_unfulfilled_research(
    intent_info: dict[str, Any] | None,
    research_result: dict[str, Any] | None,
) -> tuple[bool, str]:
    if not isinstance(intent_info, dict):
        return False, ""
    ext_req = bool(
        intent_info.get("external_research_request")
        or intent_info.get("external_research")
        or intent_info.get("needs_external_research")
    )
    if not ext_req:
        return False, ""
    if _did_research_attach(research_result):
        return False, ""
    delib = intent_info.get("deliberation_info")
    topic_origin = str(
        intent_info.get("topic_origin")
        or (delib.get("topic_origin") if isinstance(delib, dict) else "")
        or ""
    ).strip()
    return True, topic_origin


async def register_research_followup_if_needed(
    store: Any,
    *,
    user_text: str,
    channel_id: int | str | None,
    target_user_id: int | str | None,
    guild_id: int | str | None = None,
    persona: str | None = None,
    intent_info: dict[str, Any] | None = None,
    research_result: dict[str, Any] | None = None,
) -> int | None:
    """返信後の対象発言を判定し、必要なら research_followups へ登録する。"""
    if not cfg_bool("RESEARCH_FOLLOWUP_ENABLED", DEFAULT_RESEARCH_FOLLOWUP_ENABLED):
        return None
    if store is None or channel_id is None or target_user_id is None:
        return None

    clean_text = str(user_text or "").strip()
    if not clean_text:
        return None

    explicit_flag = 0
    query = ""
    if is_explicit_research_request(clean_text):
        explicit_flag = 1
        query = extract_research_query(clean_text) or clean_text
    else:
        is_implicit, topic_origin = _is_implicit_unfulfilled_research(intent_info, research_result)
        if not is_implicit:
            return None
        explicit_flag = 0
        query = topic_origin or clean_text

    resolved_persona = (
        str(persona).strip()
        if persona is not None and str(persona).strip()
        else (cfg_persona_namespace(custom_cfg=cfg) if callable(cfg_persona_namespace) else "default")
    )
    max_per_user = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_MAX_PER_USER", DEFAULT_RESEARCH_FOLLOWUP_MAX_PER_USER) or DEFAULT_RESEARCH_FOLLOWUP_MAX_PER_USER),
    )

    active_records = await list_user_active_research_followups(
        store,
        persona=resolved_persona,
        target_user_id=target_user_id,
        limit=max(max_per_user * 2, 20),
    )
    if len(active_records) >= max_per_user:
        log.info(
            "[research_followup] skipped registration: user=%s reached max active followups (%d >= %d)",
            target_user_id,
            len(active_records),
            max_per_user,
        )
        return None

    for rec in active_records:
        existing_q = str(rec.get("query") or "").strip()
        if existing_q and _is_duplicate_query(query, existing_q):
            log.info(
                "[research_followup] skipped registration: duplicate query=%r overlaps existing=%r for user=%s",
                query,
                existing_q,
                target_user_id,
            )
            return None

    request_text = clean_text[:200]
    record_id = await add_research_followup(
        store,
        persona=resolved_persona,
        guild_id=guild_id,
        channel_id=channel_id,
        target_user_id=target_user_id,
        request_text=request_text,
        query=query,
        explicit=explicit_flag,
    )
    log.info(
        "[research_followup] registered followup id=%s user=%s channel=%s explicit=%d query=%r",
        record_id,
        target_user_id,
        channel_id,
        explicit_flag,
        query,
    )
    return record_id


def schedule_research_followup_from_turn(
    cog: Any,
    *,
    message: Any,
    runtime: Any,
    intent_info: dict[str, Any] | None = None,
    research_result: dict[str, Any] | None = None,
) -> asyncio.Task[Any] | None:
    """返信送信後の後処理から非同期タスクとして登録を起動する（返信処理を遅らせない）。"""
    try:
        if not cfg_bool("RESEARCH_FOLLOWUP_ENABLED", DEFAULT_RESEARCH_FOLLOWUP_ENABLED):
            return None
        store = getattr(cog, "memory_store", None)
        if store is None:
            return None

        user_text = str(
            getattr(runtime, "original_user_text", "")
            or getattr(runtime, "effective_user_text", "")
            or getattr(message, "content", "")
            or ""
        ).strip()
        if not user_text:
            return None

        cid = getattr(runtime, "cid", None) or getattr(getattr(message, "channel", None), "id", None)
        uid = getattr(getattr(message, "author", None), "id", None) or getattr(runtime, "user_id", None)
        gid = getattr(getattr(message, "guild", None), "id", None)
        if cid is None or uid is None:
            return None

        async def _runner() -> None:
            try:
                await register_research_followup_if_needed(
                    store,
                    user_text=user_text,
                    channel_id=cid,
                    target_user_id=uid,
                    guild_id=gid,
                    intent_info=intent_info,
                    research_result=research_result,
                )
            except Exception as exc:
                log.warning("[research_followup] background registration failed: %r", exc)
                report_error("research_followup.register", exc)

        return asyncio.create_task(_runner())
    except Exception as exc:
        log.warning("[research_followup] failed to schedule registration task: %r", exc)
        return None


# ---------------------------------------------------------------------------
# Step 3: 裏調査フェーズ
# ---------------------------------------------------------------------------


async def _summarize_search_entries(query: str, entries: list[dict[str, Any]]) -> str:
    lines: list[str] = [f"【調査テーマ】: {query}", "【検索結果】"]
    for idx, entry in enumerate(entries[:4], start=1):
        title = str(entry.get("title") or "").strip()
        snippet = str(entry.get("body") or entry.get("snippet") or entry.get("content_excerpt") or "").strip()
        url = str(entry.get("url") or entry.get("href") or "").strip()
        lines.append(f"[{idx}] {title}\nURL: {url}\n抜粋: {snippet}")
    lines.append(f"\n【指示】\n{_RESEARCH_MEMO_PROMPT_INSTRUCTION}")
    prompt = "\n".join(lines)

    middle_model = str(cfg("OLLAMA_MIDDLE_MODEL", "middle-llm") or "middle-llm").strip()
    try:
        res = await call_ollama_json(
            prompt,
            system_prompt=_RESEARCH_MEMO_PROMPT_INSTRUCTION,
            schema=_RESEARCH_MEMO_SCHEMA,
            model=middle_model,
            temperature=0.0,
            think=False,
        )
        if isinstance(res, dict):
            memo = str(res.get("findings") or res.get("summary") or res.get("memo") or "").strip()
            if memo:
                return memo
        elif isinstance(res, str) and res.strip():
            return res.strip()
    except Exception as exc:
        log.warning("[research_followup] middle model summary failed for query=%r: %r", query, exc)

    # フォールバック（検索結果抜粋の連結）
    fallback_parts: list[str] = []
    for entry in entries[:2]:
        title = str(entry.get("title") or "").strip()
        snippet = str(entry.get("body") or entry.get("snippet") or "").strip()
        if snippet:
            fallback_parts.append(f"{title}: {snippet}")
        elif title:
            fallback_parts.append(title)
    return " / ".join(fallback_parts)[:400] or f"{query}に関する検索結果を確認しました。"


async def run_research_phase(
    store: Any,
    *,
    persona: str = "default",
    now: datetime | None = None,
    is_busy_fn: Any = None,
) -> int:
    """未調査（pending）のフォローアップを最大2件引き受け、Web検索＋中間モデル要約を行う。"""
    now_jst = _to_jst(now or datetime.now(_JST))
    pending = await list_pending_research_followups(store, persona=persona, limit=2)
    processed = 0

    min_delay_hours = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_MIN_DELAY_HOURS", DEFAULT_RESEARCH_FOLLOWUP_MIN_DELAY_HOURS) or DEFAULT_RESEARCH_FOLLOWUP_MIN_DELAY_HOURS),
    )

    for rec in pending[:2]:
        if callable(is_busy_fn) and is_busy_fn():
            log.info("[research_followup] user reply became busy during research phase; yielding")
            break

        rec_id_raw = rec.get("id")
        if rec_id_raw is None:
            continue
        rec_id = int(rec_id_raw)
        query = str(rec.get("query") or rec.get("request_text") or "").strip()
        if not query:
            await mark_research_followup_failed(store, rec_id)
            continue

        entries: list[dict[str, Any]] = []
        try:
            raw_entries = await search_web_entries_unified(query, max_results=4)
            if isinstance(raw_entries, list):
                entries = [e for e in raw_entries if isinstance(e, dict)]
        except Exception as exc:
            log.warning("[research_followup] search_web_entries_unified failed for id=%s query=%r: %r", rec_id, query, exc)
            entries = []

        if not entries:
            new_attempts = await increment_research_followup_attempts(store, rec_id)
            if new_attempts <= 0:
                new_attempts = int(rec.get("attempts") or 0) + 1
            if new_attempts >= 2:
                await mark_research_followup_failed(store, rec_id)
                log.info("[research_followup] marked failed id=%s query=%r (attempts=%d)", rec_id, query, new_attempts)
            else:
                log.info("[research_followup] search empty id=%s query=%r (attempts=%d; will retry)", rec_id, query, new_attempts)
            continue

        findings = await _summarize_search_entries(query, entries)
        sources: list[dict[str, str]] = []
        for entry in entries[:4]:
            url = str(entry.get("url") or entry.get("href") or "").strip()
            title = str(entry.get("title") or "").strip()
            if url:
                sources.append({"title": title, "url": url})

        deliver_after_dt = now_jst + timedelta(hours=min_delay_hours)
        await mark_research_followup_researched(
            store,
            rec_id,
            findings=findings,
            sources_json=sources,
            deliver_after=deliver_after_dt.isoformat(),
            now_iso=now_jst.isoformat(),
        )
        processed += 1
        log.info(
            "[research_followup] researched id=%s query=%r deliver_after=%s sources=%d",
            rec_id,
            query,
            deliver_after_dt.isoformat(),
            len(sources),
        )

    return processed


# ---------------------------------------------------------------------------
# Step 4: 配達フェーズ
# ---------------------------------------------------------------------------


def _parse_sources_list(raw_sources: Any) -> list[dict[str, str]]:
    if not raw_sources:
        return []
    items: list[Any] = []
    if isinstance(raw_sources, list):
        items = raw_sources
    elif isinstance(raw_sources, str):
        try:
            parsed = json.loads(raw_sources)
            if isinstance(parsed, list):
                items = parsed
        except Exception:
            return []
    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("href") or "").strip()
            title = str(item.get("title") or "").strip()
            if url:
                result.append({"title": title, "url": url})
    return result


async def _fetch_channel_activity(
    channel: Any,
    *,
    target_user_id: str | int,
    bot_user_id: int | str | None = None,
    limit: int = 30,
) -> tuple[datetime | None, datetime | None, Any]:
    """直近の履歴（最大30件）から (user_last_active_at, channel_last_active_at, last_user_message) を取得する。"""
    messages: list[Any] = []
    history_fn = getattr(channel, "history", None)
    if callable(history_fn):
        try:
            hist_obj = history_fn(limit=limit)
            if hasattr(hist_obj, "__aiter__"):
                messages = [m async for m in hist_obj]
            elif inspect.isawaitable(hist_obj):
                res = await hist_obj
                if isinstance(res, Iterable):
                    messages = list(res)
            elif isinstance(hist_obj, Iterable):
                messages = list(hist_obj)
        except Exception as exc:
            log.warning("[research_followup] channel.history failed: %r", exc)

    user_last_active_at: datetime | None = getattr(channel, "user_last_active_at", None)
    channel_last_active_at: datetime | None = getattr(channel, "channel_last_active_at", None)
    last_user_message: Any = getattr(channel, "last_user_message", None)

    target_uid_str = str(target_user_id).strip()
    bot_uid_str = str(bot_user_id).strip() if bot_user_id is not None else ""

    for msg in messages:
        created_at = _parse_iso_datetime(getattr(msg, "created_at", None))
        author = getattr(msg, "author", None)
        author_uid_str = str(getattr(author, "id", "") or "").strip()
        is_bot = bool(getattr(author, "bot", False)) or (bot_uid_str and author_uid_str == bot_uid_str)

        if created_at is not None and not is_bot:
            if channel_last_active_at is None or _to_jst(created_at) > _to_jst(channel_last_active_at):
                channel_last_active_at = created_at

        if author_uid_str and author_uid_str == target_uid_str:
            if last_user_message is None:
                last_user_message = msg
            if created_at is not None:
                if user_last_active_at is None or _to_jst(created_at) > _to_jst(user_last_active_at):
                    user_last_active_at = created_at
                    last_user_message = msg

    if channel_last_active_at is None and user_last_active_at is not None:
        channel_last_active_at = user_last_active_at

    return user_last_active_at, channel_last_active_at, last_user_message


async def _get_user_delivery_stats_today(
    store: Any,
    *,
    persona: str,
    target_user_id: str | int,
    now_jst: datetime,
) -> tuple[int, int]:
    """当日の (配達件数, メンション配達件数) を返す。"""
    today_prefix = now_jst.strftime("%Y-%m-%d")
    cursor_fn = getattr(store, "_cursor", None)
    if callable(cursor_fn):
        try:
            async with cursor_fn() as cur:
                await cur.execute(
                    """
                    SELECT mention, delivered_at
                    FROM research_followups
                    WHERE persona = ?
                      AND target_user_id = ?
                      AND status = 'delivered'
                      AND delivered_at IS NOT NULL
                    """,
                    (persona, str(target_user_id).strip()),
                )
                rows = list(await cur.fetchall())
                total_today = 0
                mentions_today = 0
                for row in rows:
                    deliv_dt = _parse_iso_datetime(row["delivered_at"])
                    if deliv_dt is not None and deliv_dt.strftime("%Y-%m-%d") == today_prefix:
                        total_today += 1
                        if row["mention"]:
                            mentions_today += 1
                return total_today, mentions_today
        except Exception as exc:
            log.debug("[research_followup] _get_user_delivery_stats_today query skipped: %r", exc)

    total_today = int(getattr(store, "deliveries_today", 0) or 0)
    mentions_today = int(getattr(store, "mentions_today", 0) or 0)
    return total_today, mentions_today


async def _generate_delivery_message(
    cog: Any,
    *,
    request_text: str,
    findings: str,
    sources: list[dict[str, str]],
) -> str:
    sys_prompt = ""
    sys_prompt_fn = getattr(cog, "_reply_system_prompt", None)
    if callable(sys_prompt_fn):
        with contextlib.suppress(Exception):
            sys_prompt = str(sys_prompt_fn() or "")

    source_lines = [
        f"- {s.get('title') or '出典'}: {s.get('url')}"
        for s in sources[:2]
        if s.get("url")
    ]
    sources_block = "\n".join(source_lines) if source_lines else "（出典リンクなし）"

    prompt = (
        f"【元の話題・依頼】\n{request_text}\n\n"
        f"【調査で分かった内容】\n{findings}\n\n"
        f"【出典】\n{sources_block}\n\n"
        f"【指示】\n{_DELIVERY_PROMPT_INSTRUCTION}"
    )

    call_model_fn = getattr(cog, "_call_model", None)
    raw_text = ""
    if callable(call_model_fn):
        res = call_model_fn(prompt, system_prompt=sys_prompt)
        raw_text = str((await res) if inspect.isawaitable(res) else res or "")
    else:
        model_name = str(cfg("OLLAMA_MODEL", "") or "").strip()
        raw_text = str(
            await call_ollama(
                prompt,
                system_prompt=sys_prompt,
                model=model_name,
            )
            or ""
        )

    cleaned = sanitize_generated_reply(raw_text).strip()
    return cleaned


async def _resolve_discord_channel(cog: Any, channel_id_val: str | int) -> Any:
    bot = getattr(cog, "bot", cog)
    if bot is None:
        return None
    cid_lookup: int | str = channel_id_val
    if isinstance(channel_id_val, str) and channel_id_val.isdigit():
        cid_lookup = int(channel_id_val)

    get_ch = getattr(bot, "get_channel", None)
    channel = get_ch(cid_lookup) if callable(get_ch) else None
    if channel is None:
        fetch_ch = getattr(bot, "fetch_channel", None)
        if callable(fetch_ch):
            with contextlib.suppress(Exception):
                res = fetch_ch(cid_lookup)
                channel = (await res) if inspect.isawaitable(res) else res
    return channel


async def run_delivery_phase(
    cog: Any,
    store: Any,
    *,
    persona: str = "default",
    now: datetime | None = None,
    is_busy_fn: Any = None,
) -> int:
    """配達可能（researched かつ deliver_after 到来）な件をチャンネルごとに最大1件配達する。"""
    now_jst = _to_jst(now or datetime.now(_JST))
    deliverables = await list_deliverable_research_followups(
        store,
        persona=persona,
        now_iso=now_jst.isoformat(),
        limit=10,
    )
    if not deliverables:
        return 0

    max_per_day = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_MAX_DELIVERIES_PER_DAY", DEFAULT_RESEARCH_FOLLOWUP_MAX_DELIVERIES_PER_DAY) or DEFAULT_RESEARCH_FOLLOWUP_MAX_DELIVERIES_PER_DAY),
    )

    seen_channels: set[str] = set()
    user_stats_cache: dict[str, tuple[int, int]] = {}
    delivered_count = 0

    bot = getattr(cog, "bot", None)
    bot_user_id = getattr(getattr(bot, "user", None), "id", None)

    for rec in deliverables:
        if callable(is_busy_fn) and is_busy_fn():
            log.info("[research_followup] user reply became busy during delivery phase; yielding")
            break

        cid_raw = rec.get("channel_id")
        uid_raw = rec.get("target_user_id")
        rec_id_raw = rec.get("id")
        if cid_raw is None or uid_raw is None or rec_id_raw is None:
            continue

        cid_key = str(cid_raw).strip()
        uid_key = str(uid_raw).strip()
        rec_id = int(rec_id_raw)
        if not cid_key or cid_key in seen_channels:
            continue

        deliver_after_dt = _parse_iso_datetime(rec.get("deliver_after")) or now_jst
        explicit = bool(rec.get("explicit"))

        if uid_key not in user_stats_cache:
            user_stats_cache[uid_key] = await _get_user_delivery_stats_today(
                store,
                persona=persona,
                target_user_id=uid_key,
                now_jst=now_jst,
            )
        deliveries_today, mentions_today = user_stats_cache[uid_key]
        if deliveries_today >= max_per_day:
            log.info(
                "[research_followup] skipping delivery id=%s: user=%s reached daily limit (%d >= %d)",
                rec_id,
                uid_key,
                deliveries_today,
                max_per_day,
            )
            continue

        channel = await _resolve_discord_channel(cog, cid_raw)
        if channel is None or not hasattr(channel, "send"):
            continue

        user_last_active_at, channel_last_active_at, last_user_message = await _fetch_channel_activity(
            channel,
            target_user_id=uid_key,
            bot_user_id=bot_user_id,
            limit=30,
        )

        if not should_deliver_now(
            explicit=explicit,
            now=now_jst,
            deliver_after=deliver_after_dt,
            user_last_active_at=user_last_active_at,
            channel_last_active_at=channel_last_active_at,
        ):
            continue

        seen_channels.add(cid_key)
        use_mention = decide_mention(
            explicit=explicit,
            now=now_jst,
            user_last_active_at=user_last_active_at,
            mentions_today=mentions_today,
        )

        sources = _parse_sources_list(rec.get("sources_json"))
        speech_text = await _generate_delivery_message(
            cog,
            request_text=str(rec.get("request_text") or ""),
            findings=str(rec.get("findings") or ""),
            sources=sources,
        )
        if not speech_text:
            log.warning("[research_followup] generated empty delivery message for id=%s", rec_id)
            continue

        try:
            if use_mention:
                if last_user_message is not None and hasattr(last_user_message, "reply"):
                    await last_user_message.reply(speech_text, mention_author=True)
                else:
                    mention_prefix = f"<@{uid_key}> "
                    body = speech_text if speech_text.startswith("<@") else f"{mention_prefix}{speech_text}"
                    await channel.send(body)
            else:
                await channel.send(speech_text)
        except Exception as exc:
            log.warning("[research_followup] failed to send delivery id=%s in channel=%s: %r", rec_id, cid_key, exc)
            continue

        await mark_research_followup_delivered(
            store,
            rec_id,
            mention=use_mention,
            now_iso=now_jst.isoformat(),
        )
        user_stats_cache[uid_key] = (
            deliveries_today + 1,
            mentions_today + (1 if use_mention else 0),
        )
        delivered_count += 1
        log.info(
            "[research_followup] delivered id=%s channel=%s user=%s mention=%s",
            rec_id,
            cid_key,
            uid_key,
            use_mention,
        )

    return delivered_count


async def run_research_followup_cycle(
    cog: Any,
    *,
    now: datetime | None = None,
) -> None:
    """背景ループの1周期処理（キルスイッチ・ビジー判定・期限切れ・調査・配達）。"""
    if not cfg_bool("RESEARCH_FOLLOWUP_ENABLED", DEFAULT_RESEARCH_FOLLOWUP_ENABLED):
        return

    is_busy_fn = getattr(cog, "_is_user_reply_busy", None)
    if callable(is_busy_fn) and is_busy_fn():
        log.info("_research_followup_loop: user reply is busy; yielding cycle")
        return

    store = getattr(cog, "memory_store", None)
    if store is None:
        return

    now_jst = _to_jst(now or datetime.now(_JST))
    persona = (
        cfg_persona_namespace(custom_cfg=cfg)
        if callable(cfg_persona_namespace)
        else str(cfg("OLLAMA_BOT_PERSONA", "default") or "default")
    )

    expire_days = max(
        1,
        int(cfg_int("RESEARCH_FOLLOWUP_EXPIRE_DAYS", DEFAULT_RESEARCH_FOLLOWUP_EXPIRE_DAYS) or DEFAULT_RESEARCH_FOLLOWUP_EXPIRE_DAYS),
    )
    cutoff_iso = (now_jst - timedelta(days=expire_days)).isoformat()
    await expire_research_followups(store, persona=persona, cutoff_iso=cutoff_iso)

    await run_research_phase(
        store,
        persona=persona,
        now=now_jst,
        is_busy_fn=is_busy_fn,
    )

    if callable(is_busy_fn) and is_busy_fn():
        log.info("_research_followup_loop: user reply became busy before delivery; yielding")
        return

    await run_delivery_phase(
        cog,
        store,
        persona=persona,
        now=now_jst,
        is_busy_fn=is_busy_fn,
    )


async def _research_followup_loop(cog: Any) -> None:
    """events.py から呼ばれる後日調査フォローアップ背景ループ本体。"""
    bot = getattr(cog, "bot", None)
    if bot is not None and hasattr(bot, "wait_until_ready"):
        await bot.wait_until_ready()

    while bot is None or not bot.is_closed():
        loop_minutes = max(
            1,
            int(cfg_int("RESEARCH_FOLLOWUP_LOOP_MINUTES", DEFAULT_RESEARCH_FOLLOWUP_LOOP_MINUTES) or DEFAULT_RESEARCH_FOLLOWUP_LOOP_MINUTES),
        )
        await asyncio.sleep(float(loop_minutes * 60))
        try:
            await run_research_followup_cycle(cog)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("_research_followup_loop error: %r", exc)
            report_error("events._research_followup_loop", exc)


run_research_followup_loop = _research_followup_loop

__all__ = [
    "_EXPLICIT_RESEARCH_RE",
    "is_explicit_research_request",
    "extract_research_query",
    "decide_mention",
    "should_deliver_now",
    "register_research_followup_if_needed",
    "schedule_research_followup_from_turn",
    "run_research_phase",
    "run_delivery_phase",
    "run_research_followup_cycle",
    "_research_followup_loop",
    "run_research_followup_loop",
]
