"""ハーネス共通サブエージェント基盤（文脈分離つき LLM 委譲）。

親エージェントや各フロントから独立した単発の分析・判定・抽出タスク（子エージェント）へ
仕事を委ね、構造化データまたはテキストの結果だけを受け取るための共通モジュール。

【設計前提（Design Assumptions）】
- **VRAM 16GB 環境における直列実行（既定同時実行数 = 1）**:
  本 Bot の実行環境は VRAM 16GB であり、LLM モデルは一度に 1 つずつしか稼働できない。
  そのため本基盤の狙いは「並列化」ではなく、モジュールレベルのセマフォにより既定 1 件（直列）で実行する。
- **本基盤の 3 つの利点**:
  1. **文脈の分離**: 子タスクは親の会話履歴を持たず、渡された `system_prompt` と `user_prompt` だけで動作する。
     親の文脈汚染が子へ、子の失敗が親へ伝播しない。
  2. **失敗の局所化**: タイムアウト（`timeout_sec`）および全ての例外を子の境界で閉じ、
     呼び出し側には例外を伝播させず `SubagentResult(ok, data, error, elapsed_sec)` として成否と理由だけを返す。
  3. **記録の統一**: 子タスクの実行は `purpose="subagent:<name>"` として開始・終了ログおよび
     行動台帳（`add_bot_activity` が利用可能な場合）に目的名つきで記録され、後から追跡できる
     （プライバシー・ログ肥大化防止のためプロンプト本文はログに出力しない）。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import inspect
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_config_helpers: Any = None
_default_cfg: Callable[..., Any] | None = None
_default_cfg_int: Callable[..., Any] | None = None
try:
    import ollama_bot.common.config_helpers as _imported_config_helpers
    from ollama_bot.common.config_helpers import (
        cfg as _imported_cfg,
        cfg_int as _imported_cfg_int,
    )

    _config_helpers = _imported_config_helpers
    _default_cfg = _imported_cfg
    _default_cfg_int = _imported_cfg_int
except Exception:  # pragma: no cover
    pass

cfg: Callable[..., Any] | None = _default_cfg
cfg_int: Callable[..., Any] | None = _default_cfg_int

_ollama_helpers: Any = None
_default_call_ollama: Callable[..., Any] | None = None
_default_call_ollama_json: Callable[..., Any] | None = None
try:
    import ollama_bot.common.ollama_helpers as _imported_ollama_helpers
    from ollama_bot.common.ollama_helpers import (
        call_ollama as _imported_call_ollama,
        call_ollama_json as _imported_call_ollama_json,
    )

    _ollama_helpers = _imported_ollama_helpers
    _default_call_ollama = _imported_call_ollama
    _default_call_ollama_json = _imported_call_ollama_json
except Exception:  # pragma: no cover
    pass

call_ollama: Callable[..., Any] | None = _default_call_ollama
call_ollama_json: Callable[..., Any] | None = _default_call_ollama_json

_default_add_bot_activity: Callable[..., Any] | None = None
for _mod_name in (
    "harness.agent.memory_manager",
    "ollama_bot.common.activity_log",
    "ollama_bot.common.memory_helpers",
):
    try:
        _mod = importlib.import_module(_mod_name)
        _cand = getattr(_mod, "add_bot_activity", None)
        if callable(_cand):
            _default_add_bot_activity = _cand
            break
    except Exception:  # pragma: no cover
        continue

add_bot_activity: Callable[..., Any] | None = _default_add_bot_activity


# ============================================================================
# データ型定義
# ============================================================================
@dataclass
class SubagentTask:
    name: str                                # 目的名（ログ・予算の purpose に使う）
    system_prompt: str
    user_prompt: str
    model: str | None = None                 # None なら呼び出し側の既定（middle-llm 相当の解決はヘルパーに委ねる）
    schema: dict[str, Any] | None = None     # 指定時は JSON モード（call_ollama_json）
    timeout_sec: float = 60.0
    temperature: float = 0.2


@dataclass
class SubagentResult:
    ok: bool
    data: Any                                # JSON モードでは dict、テキストモードでは str
    error: str                               # 失敗理由（成功時は空文字）
    elapsed_sec: float


# ============================================================================
# 同時実行制御（セマフォ）と設定解決
# ============================================================================
_subagent_semaphore: asyncio.Semaphore | None = None
_subagent_semaphore_limit: int | None = None
_subagent_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _resolve_max_concurrency(raw_override: Any = None) -> int:
    """SUBAGENT_MAX_CONCURRENCY を解決し、最小 1 に丸める（0 や負数 → 1）。"""
    raw: Any = raw_override
    if raw is None:
        raw = 1
        try:
            fn = cfg_int
            if fn is _default_cfg_int and _config_helpers is not None:
                fn = getattr(_config_helpers, "cfg_int", fn)
            if callable(fn):
                raw = fn("SUBAGENT_MAX_CONCURRENCY", 1)
            else:
                cfg_fn = cfg
                if cfg_fn is _default_cfg and _config_helpers is not None:
                    cfg_fn = getattr(_config_helpers, "cfg", cfg_fn)
                if callable(cfg_fn):
                    raw = cfg_fn("SUBAGENT_MAX_CONCURRENCY", 1)
        except Exception:
            raw = 1
    try:
        val = int(raw)
    except (TypeError, ValueError):
        val = 1
    return max(val, 1)


def _get_subagent_semaphore() -> asyncio.Semaphore:
    """モジュールレベルのセマフォを取得する（設定値またはイベントループ変更時は再生成）。"""
    global _subagent_semaphore
    global _subagent_semaphore_limit
    global _subagent_semaphore_loop

    limit = _resolve_max_concurrency()
    try:
        current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None

    if (
        _subagent_semaphore is None
        or _subagent_semaphore_limit != limit
        or (current_loop is not None and _subagent_semaphore_loop is not current_loop)
    ):
        _subagent_semaphore = asyncio.Semaphore(limit)
        _subagent_semaphore_limit = limit
        _subagent_semaphore_loop = current_loop
    return _subagent_semaphore


def _resolve_default_model() -> str:
    """model=None 時のフォールバックモデル名を解決する。"""
    try:
        cfg_fn = cfg
        if cfg_fn is _default_cfg and _config_helpers is not None:
            cfg_fn = getattr(_config_helpers, "cfg", cfg_fn)
        if callable(cfg_fn):
            for key in ("OLLAMA_UTILITY_MODEL", "OLLAMA_CLASSIFIER_MODEL", "OLLAMA_MODEL"):
                val = str(cfg_fn(key, "") or "").strip()
                if val:
                    return val
    except Exception:
        pass
    return ""


def _resolve_call_ollama() -> Callable[..., Any]:
    fn = call_ollama
    if fn is _default_call_ollama and _ollama_helpers is not None:
        fn = getattr(_ollama_helpers, "call_ollama", fn)
    if fn is None:
        from ollama_bot.common.ollama_helpers import call_ollama as imported_fn

        fn = imported_fn
    if not callable(fn):
        raise RuntimeError("call_ollama helper is not available")
    return fn


def _resolve_call_ollama_json() -> Callable[..., Any]:
    fn = call_ollama_json
    if fn is _default_call_ollama_json and _ollama_helpers is not None:
        fn = getattr(_ollama_helpers, "call_ollama_json", fn)
    if fn is None:
        from ollama_bot.common.ollama_helpers import call_ollama_json as imported_fn

        fn = imported_fn
    if not callable(fn):
        raise RuntimeError("call_ollama_json helper is not available")
    return fn


def _accepts_keyword_arg(fn: Any, arg_name: str) -> bool:
    """ヘルパー関数が指定されたキーワード引数を明示的に受け取るか判定する。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    param = sig.parameters.get(arg_name)
    if param is None:
        return False
    return param.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _summarize_exception(exc: BaseException) -> str:
    """例外を簡潔な1行の要約文字列に整形する。"""
    msg = str(exc).strip()
    summary = f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__
    return summary[:300]


async def _record_subagent_activity(
    *,
    name: str,
    ok: bool,
    elapsed_sec: float,
) -> None:
    """add_bot_activity が利用可能な場合のみ、行動台帳へ subagent 実行要約を記録する。"""
    fn = add_bot_activity
    if fn is None:
        return
    if not callable(fn):
        return

    summary = f"subagent:{name} ok={ok} elapsed={elapsed_sec:.2f}s"
    try:
        kwargs: dict[str, Any] = {
            "kind": "subagent",
            "summary": summary,
        }
        try:
            sig = inspect.signature(fn)
            if "persona" in sig.parameters:
                kwargs["persona"] = "default"
            if "front" in sig.parameters:
                kwargs["front"] = "harness"
        except (TypeError, ValueError):
            pass

        res = fn(**kwargs)
        if inspect.isawaitable(res):
            await res
    except Exception as exc:
        logger.debug("subagent activity recording skipped: %s", exc)


async def _invoke_llm(task: SubagentTask, purpose: str) -> Any:
    """task.schema の有無に応じて call_ollama_json または call_ollama を呼び出す。"""
    if task.schema is not None:
        json_fn = _resolve_call_ollama_json()
        resolved_model = task.model if task.model is not None else _resolve_default_model()
        json_kwargs: dict[str, Any] = {
            "system_prompt": task.system_prompt,
            "model": resolved_model,
            "schema": task.schema,
            "timeout_sec": task.timeout_sec,
            "temperature": task.temperature,
        }
        if _accepts_keyword_arg(json_fn, "purpose"):
            json_kwargs["purpose"] = purpose
        return await json_fn(task.user_prompt, **json_kwargs)

    text_fn = _resolve_call_ollama()
    text_kwargs: dict[str, Any] = {
        "system_prompt": task.system_prompt,
        "model": task.model,
        "timeout_sec": task.timeout_sec,
        "temperature": task.temperature,
    }
    if _accepts_keyword_arg(text_fn, "purpose"):
        text_kwargs["purpose"] = purpose
    return await text_fn(task.user_prompt, **text_kwargs)


# ============================================================================
# サブエージェント実行エントリポイント
# ============================================================================
async def run_subagent(task: SubagentTask) -> SubagentResult:
    """文脈を分離した単発 LLM サブエージェントタスクを実行し、SubagentResult を返す。"""
    purpose = f"subagent:{task.name}"
    mode = "json" if task.schema is not None else "text"
    semaphore = _get_subagent_semaphore()

    start_time = time.monotonic()
    logger.info(
        "subagent start: name=%s purpose=%s mode=%s timeout_sec=%.1f",
        task.name,
        purpose,
        mode,
        float(task.timeout_sec),
    )

    ok = False
    data: Any = None
    error = ""

    try:
        async with semaphore:
            data = await asyncio.wait_for(
                _invoke_llm(task, purpose),
                timeout=float(task.timeout_sec),
            )
            ok = True
    except (asyncio.TimeoutError, TimeoutError):
        ok = False
        data = None
        error = "timeout"
    except Exception as exc:
        ok = False
        data = None
        error = _summarize_exception(exc)

    elapsed_sec = max(time.monotonic() - start_time, 0.0)
    logger.info(
        "subagent finish: name=%s purpose=%s ok=%s elapsed_sec=%.3f%s",
        task.name,
        purpose,
        ok,
        elapsed_sec,
        f" error={error}" if error else "",
    )

    await _record_subagent_activity(
        name=task.name,
        ok=ok,
        elapsed_sec=elapsed_sec,
    )

    return SubagentResult(
        ok=ok,
        data=data,
        error=error,
        elapsed_sec=elapsed_sec,
    )


__all__ = [
    "SubagentTask",
    "SubagentResult",
    "run_subagent",
]
