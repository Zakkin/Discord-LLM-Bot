"""AIエージェント・ハーネス オーケストレーター Bot出力内容の背景評価 (orchestrator/output_evaluation.py)

§156 第1.5段階（評価の記録）。
全フロント共通行動台帳（bot_activities）に貯まった Bot 自身の出力（kind="reply" / "post"）を
背景ジョブで middle 系モデルにより分類し、内容ラベルを kind="reply_eval" の別レコードとして
元の発言に紐付けて記録する。送信経路には一切関与しない。

記録形式（§155・§157 と共通の契約）:
- kind="reply_eval"、front / persona は対象レコードと同じ値
- summary: 対象レコードの summary（最大400字）
- detail: JSON 文字列（最大800字）{"target_ref", "target_kind", "stance", "labels"}

延期・fail-open 方針:
- §133 会話活動レジストリが「会話継続中」を示す間はその回を丸ごと見送る（対象を消費しない）。
  レジストリが参照できない場合は見送らずに実行する。
- モデル名は config の OLLAMA_MIDDLE_MODEL 経由でのみ解決し、解決できなければその回をスキップする。
- パース失敗の件は記録せず、次回の実行で再試行できる状態のまま残す。
- 例外はすべて握り、呼び出し元ループを停止させない。
"""
from __future__ import annotations

import inspect
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from lib.config_utils import (
    cfg,
    cfg_bool,
    cfg_float,
    cfg_int,
    cfg_persona_namespace,
)
from ollama_bot.common.ollama_helpers import call_ollama_json

log = logging.getLogger("harness.agent.orchestrator.output_evaluation")

REPLY_EVAL_KIND = "reply_eval"
TARGET_KINDS: tuple[str, ...] = ("reply", "post")
STANCES: tuple[str, ...] = ("positive", "negative", "neutral", "mixed")
LABELS: tuple[str, ...] = (
    "lecture",
    "gloss",
    "verification_hedge",
    "playful",
    "apology",
    "factual",
    "question_answer",
)

DEFAULT_OUTPUT_EVAL_ENABLED = True
DEFAULT_OUTPUT_EVAL_INTERVAL_SEC = 1800
DEFAULT_OUTPUT_EVAL_MAX_PER_RUN = 10
DEFAULT_OUTPUT_EVAL_TIMEOUT_SEC = 60.0
DEFAULT_OUTPUT_EVAL_NUM_PREDICT = 256
OUTPUT_EVAL_WINDOW_HOURS = 24.0
_SCAN_LIMIT = 500
_SUMMARY_MAX_CHARS = 400
_DETAIL_MAX_CHARS = 800

OUTPUT_EVAL_SYSTEM_PROMPT = (
    "あなたはチャットBotの発言を分類する評価器です。"
    "与えられたBotの発言1件だけを読み、指定のJSONで分類結果を返してください。説明文は不要です。\n"
    "stance（その発言が相手や話題に対して好意的か否定的か。いずれか1つ）:\n"
    "- positive: 好意的・肯定的・共感的\n"
    "- negative: 否定的・批判的・突き放す\n"
    "- neutral: どちらでもない・淡々としている\n"
    "- mixed: 好意と否定が混在している\n"
    "labels（該当するものをすべて。該当なしは空配列）:\n"
    "- lecture: 講釈・用語解説調で、頼まれていない説明を長々としている\n"
    "- gloss: 言葉の意味（語釈）を説明するところから入っている\n"
    "- verification_hedge: 確認できない・分からない旨の留保をしている\n"
    "- playful: 冗談・ノリ・軽口\n"
    "- apology: 謝罪している\n"
    "- factual: 事実や情報を答えている\n"
    "- question_answer: 相手の質問に直接答えている"
)

_OUTPUT_EVAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": list(STANCES)},
        "labels": {"type": "array", "items": {"type": "string", "enum": list(LABELS)}},
    },
    "required": ["stance", "labels"],
}

CallJsonFn = Callable[..., Awaitable[Any]]


def is_output_evaluation_enabled() -> bool:
    """OUTPUT_EVAL_ENABLED（既定 True）と ACTIVITY_LOG_ENABLED の両方が有効なときのみ True。"""
    try:
        return bool(cfg_bool("OUTPUT_EVAL_ENABLED", DEFAULT_OUTPUT_EVAL_ENABLED)) and bool(
            cfg_bool("ACTIVITY_LOG_ENABLED", True)
        )
    except Exception:
        return False


def output_eval_interval_sec() -> int:
    return max(cfg_int("OUTPUT_EVAL_INTERVAL_SEC", DEFAULT_OUTPUT_EVAL_INTERVAL_SEC), 60)


def output_eval_max_per_run() -> int:
    return max(cfg_int("OUTPUT_EVAL_MAX_PER_RUN", DEFAULT_OUTPUT_EVAL_MAX_PER_RUN), 1)


def resolve_output_eval_model() -> str:
    """分類モデル（middle 系）を config の OLLAMA_MIDDLE_MODEL 経由で解決する。

    廃止済みモデル名を既定値にしない（§140）。解決できなければ空文字を返す。
    """
    try:
        return str(cfg("OLLAMA_MIDDLE_MODEL", "") or "").strip()
    except Exception:
        return ""


def _is_conversation_active() -> bool:
    """§133 会話活動レジストリで会話継続中か判定する（参照不可なら False = fail-open）。"""
    try:
        from harness.agent.prompt_parser.prompt_manager.conversation_activity import (
            is_conversation_active,
        )

        window_sec = cfg_float("CONVERSATION_ACTIVE_WINDOW_SEC", 300.0)
        return bool(is_conversation_active(window_sec=window_sec))
    except Exception as e:
        log.warning("[OUTPUT_EVAL] conversation registry unavailable; continuing (fail-open): %r", e)
        return False


def target_ref_of(row: dict[str, Any]) -> str:
    """対象レコードを一意に指す値（source_ref があればそれ、なければレコード id）。"""
    source_ref = str(row.get("source_ref") or "").strip()
    if source_ref:
        return source_ref[:200]
    return str(row.get("id") or "").strip()


def _parse_created_at(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if raw.endswith(("Z", "z")):
        raw = f"{raw[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


async def _search(memory_store: Any, **kwargs: Any) -> list[dict[str, Any]]:
    search_fn = getattr(memory_store, "search_bot_activities", None)
    if not callable(search_fn):
        return []
    rows = search_fn(**kwargs)
    if inspect.isawaitable(rows):
        rows = await rows
    if not isinstance(rows, (list, tuple)):
        return []
    return [dict(r) for r in rows if isinstance(r, dict)]


async def select_unevaluated_targets(
    memory_store: Any,
    *,
    persona: str,
    max_items: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """過去24時間の reply/post のうち、対応する reply_eval がまだないものを古い順に max_items 件返す。

    既評価の判定は同期間の reply_eval の detail.target_ref（＋同一 summary）との突き合わせで行う。
    （ふたばのように複数投稿が同じ source_ref を共有する場合でも、別投稿を既評価と誤認しないため）
    """
    current = now or datetime.now(timezone.utc)
    since_iso = (current - timedelta(hours=OUTPUT_EVAL_WINDOW_HOURS)).replace(microsecond=0).isoformat()
    targets = await _search(memory_store, persona=persona, kinds=list(TARGET_KINDS), since=since_iso, limit=_SCAN_LIMIT)
    evals = await _search(memory_store, persona=persona, kinds=[REPLY_EVAL_KIND], since=since_iso, limit=_SCAN_LIMIT * 2)

    evaluated: set[tuple[str, str]] = set()
    for ev in evals:
        try:
            detail = json.loads(str(ev.get("detail") or ""))
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict) and str(detail.get("target_ref") or "").strip():
            evaluated.add((str(detail["target_ref"]).strip(), str(ev.get("summary") or "").strip()))

    pending: list[dict[str, Any]] = []
    for row in targets:
        if str(row.get("kind") or "").strip().lower() not in TARGET_KINDS:
            continue
        summary = str(row.get("summary") or "").strip()[:_SUMMARY_MAX_CHARS]
        ref = target_ref_of(row)
        if not summary or not ref or (ref, summary) in evaluated:
            continue
        pending.append(row)
    pending.sort(key=lambda r: (_parse_created_at(r.get("created_at")), int(r.get("id") or 0)))
    return pending[: max(int(max_items), 0)]


def _normalize_classification(result: Any) -> tuple[str, list[str]] | None:
    """モデル応答を検証し (stance, labels) を返す。不正な応答は None（記録しない）。"""
    if not isinstance(result, dict):
        return None
    stance = str(result.get("stance") or "").strip().lower()
    raw_labels = result.get("labels")
    if stance not in STANCES or not isinstance(raw_labels, list):
        return None
    labels: list[str] = []
    for item in raw_labels:
        label = str(item or "").strip().lower()
        if label in LABELS and label not in labels:
            labels.append(label)
    return stance, labels


def _build_user_prompt(row: dict[str, Any], summary: str) -> str:
    kind = str(row.get("kind") or "").strip().lower()
    kind_label = "相手への返信" if kind == "reply" else "自分からの投稿"
    return (
        f"【分類対象のBotの発言（{kind_label}）】\n{summary}\n\n"
        '出力形式: {"stance": "positive|negative|neutral|mixed", "labels": [...]}'
    )


async def _classify(row: dict[str, Any], summary: str, *, model: str, call_json: CallJsonFn) -> tuple[str, list[str]] | None:
    try:
        result = await call_json(
            _build_user_prompt(row, summary),
            system_prompt=OUTPUT_EVAL_SYSTEM_PROMPT,
            schema=_OUTPUT_EVAL_SCHEMA,
            model=model,
            think=False,
            temperature=0.0,
            timeout_sec=max(cfg_float("OUTPUT_EVAL_TIMEOUT_SEC", DEFAULT_OUTPUT_EVAL_TIMEOUT_SEC), 10.0),
            num_predict=max(cfg_int("OUTPUT_EVAL_NUM_PREDICT", DEFAULT_OUTPUT_EVAL_NUM_PREDICT), 64),
            retries=0,
        )
    except Exception as e:
        log.warning("[OUTPUT_EVAL] classification failed; will retry next run: target_ref=%s err=%r", target_ref_of(row), e)
        return None
    normalized = _normalize_classification(result)
    if normalized is None:
        log.warning("[OUTPUT_EVAL] unparsable classification; will retry next run: target_ref=%s", target_ref_of(row))
    return normalized


async def _record_reply_eval(memory_store: Any, row: dict[str, Any], summary: str, stance: str, labels: list[str]) -> None:
    add_fn = getattr(memory_store, "add_bot_activity", None)
    if not callable(add_fn):
        raise RuntimeError("memory_store has no add_bot_activity")
    detail = json.dumps(
        {
            "target_ref": target_ref_of(row),
            "target_kind": str(row.get("kind") or "").strip().lower(),
            "stance": stance,
            "labels": labels,
        },
        ensure_ascii=False,
    )[:_DETAIL_MAX_CHARS]
    res = add_fn(
        persona=str(row.get("persona") or "").strip() or cfg_persona_namespace(custom_cfg=cfg),
        front=str(row.get("front") or "").strip(),
        kind=REPLY_EVAL_KIND,
        summary=summary,
        detail=detail,
        guild_id=row.get("guild_id"),
        channel_id=row.get("channel_id"),
    )
    if inspect.isawaitable(res):
        await res


async def run_output_evaluation_once(
    memory_store: Any,
    *,
    call_json: CallJsonFn | None = None,
    now: datetime | None = None,
) -> int:
    """1回分の背景評価を実行し、記録した reply_eval の件数を返す（例外は外へ出さない）。"""
    try:
        if not is_output_evaluation_enabled():
            return 0
        if memory_store is None:
            return 0
        if _is_conversation_active():
            log.info("[OUTPUT_EVAL] skipped: reason=conversation_active")
            return 0
        model = resolve_output_eval_model()
        if not model:
            log.info("[OUTPUT_EVAL] skipped: reason=no_middle_model")
            return 0

        persona = cfg_persona_namespace(custom_cfg=cfg)
        targets = await select_unevaluated_targets(
            memory_store, persona=persona, max_items=output_eval_max_per_run(), now=now
        )
        fn: CallJsonFn = call_json or call_ollama_json
        count = 0
        for idx, row in enumerate(targets):
            # 実行途中で会話が始まったら残りは消費せず次回へ回す
            if idx > 0 and _is_conversation_active():
                log.info("[OUTPUT_EVAL] stopped: reason=conversation_active remaining=%d", len(targets) - idx)
                break
            summary = str(row.get("summary") or "").strip()[:_SUMMARY_MAX_CHARS]
            classified = await _classify(row, summary, model=model, call_json=fn)
            if classified is None:
                continue
            await _record_reply_eval(memory_store, row, summary, classified[0], classified[1])
            count += 1
        if count > 0:
            log.info("[OUTPUT_EVAL] evaluated: count=%d", count)
        return count
    except Exception as e:
        log.warning("[OUTPUT_EVAL] run failed (fail-open): %r", e)
        return 0


async def maybe_run_output_evaluation(owner: Any, *, now_ts: float | None = None) -> None:
    """定期ループから呼ばれる入口。OUTPUT_EVAL_INTERVAL_SEC 経過時のみ1回分を背景実行する。

    owner の `_run_cancellable_background_task` があればそれ経由で実行し、
    ユーザー返信中の割り込み禁止・背景LLMタスク単一実行の既存ガードに乗せる。
    """
    try:
        if not is_output_evaluation_enabled():
            return
        current = time.time() if now_ts is None else float(now_ts)
        last = float(getattr(owner, "_output_eval_last_run_ts", 0.0) or 0.0)
        if last > 0.0 and (current - last) < output_eval_interval_sec():
            return
        busy_fn = getattr(owner, "_is_user_reply_busy", None)
        if callable(busy_fn) and busy_fn():
            log.info("[OUTPUT_EVAL] skipped: reason=user_reply_busy")
            return
        owner._output_eval_last_run_ts = current
        coro = run_output_evaluation_once(getattr(owner, "memory_store", None))
        runner = getattr(owner, "_run_cancellable_background_task", None)
        try:
            if callable(runner):
                res = runner(coro)
                if inspect.isawaitable(res):
                    await res
            else:
                await coro
        finally:
            # 実行されずに終わった場合も RuntimeWarning を出さない（完了済みなら close は無害）
            coro.close()
    except Exception as e:  # asyncio.CancelledError は BaseException のため握らずに伝播する
        log.warning("[OUTPUT_EVAL] scheduler failed (fail-open): %r", e)


__all__ = [
    "REPLY_EVAL_KIND",
    "TARGET_KINDS",
    "STANCES",
    "LABELS",
    "is_output_evaluation_enabled",
    "output_eval_interval_sec",
    "output_eval_max_per_run",
    "resolve_output_eval_model",
    "target_ref_of",
    "select_unevaluated_targets",
    "run_output_evaluation_once",
    "maybe_run_output_evaluation",
]
