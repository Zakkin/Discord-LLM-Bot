"""ハーネス共通フック機構（接点・判定・レジストリ）。

各フロント（Discord / ふたば等）の送信直前や記憶保存直前といった決まった接点（ポイント）に対し、
検査・書き換え・遮断ロジックを登録・実行するための共通基盤モジュール。

【契約（Contract）】
1. **同期・純粋関数（Synchronous & Pure）**:
   - フック関数（`HookFunc`）は同期の純粋関数に限る。
   - LLM 呼び出し、ネットワーク通信、ディスク/DB I/O、非同期処理を行うフックは本基盤の対象外とする。
2. **Fail-Open（例外握りつぶし・処理継続）**:
   - フック関数が例外を送出した場合は必ず握りつぶし、`WARNING` ログを出力した上で `allow` として後続処理を続行する。
   - 呼び出し側の送信・保存処理を決して止めない。例外発生時は `HookOutcome.applied` に `"名前(error)"` の形式で記録する。
3. **キルスイッチ（Kill Switch）**:
   - 設定 `HOOKS_ENABLED`（`ollama_bot.common.config_helpers.cfg_bool`、既定値 `True`）が `False` の場合、
     登録済みフックを一切実行せず即座に `allow`（`blocked=False`）の `HookOutcome` を返す。
   - `cfg_bool` の import 失敗時や参照例外時も fail-open により `True`（有効）として扱う。
4. **実行順序と連鎖（Order & Chaining）**:
   - ポイントごとに `order` 昇順（同順なら登録順）でフックを実行する。
   - `rewrite` が返された場合は `context.text` を新しい本文に差し替え、後続フックは差し替え後の本文を受け取る。
   - `block` が返された場合は即座に実行を停止し、`blocked=True` と遮断理由（`reason`）を返す。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import itertools
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

_config_helpers: Any = None
_default_cfg_bool: Callable[..., Any] | None = None
try:
    import ollama_bot.common.config_helpers as _imported_config_helpers
    from ollama_bot.common.config_helpers import cfg_bool as _imported_cfg_bool

    _config_helpers = _imported_config_helpers
    _default_cfg_bool = _imported_cfg_bool
except Exception:  # pragma: no cover
    pass

cfg_bool: Callable[..., Any] | None = _default_cfg_bool

# ============================================================================
# 接点（ポイント）定義
# ============================================================================
REPLY_PRE_SEND = "reply_pre_send"      # Discord 返信の送信直前
POST_PRE_SEND = "post_pre_send"        # ふたば投稿の書き込み直前
MEMORY_PRE_STORE = "memory_pre_store"  # 記憶の保存直前（将来用・本指示書では配線しない）


# ============================================================================
# データ型定義
# ============================================================================
@dataclass
class HookContext:
    point: str
    front: str                 # "discord" / "hutaba" など
    text: str                  # 検査対象の本文（返信文・投稿文・記憶内容）
    metadata: dict[str, Any] = field(default_factory=dict)  # フロント固有の付帯情報（任意）


@dataclass
class HookVerdict:
    action: str                # "allow" / "rewrite" / "block"
    text: str | None = None    # rewrite の場合の新しい本文
    reason: str = ""


@dataclass
class HookOutcome:
    blocked: bool
    reason: str
    text: str                  # rewrite を全て適用した後の本文
    applied: list[str] = field(default_factory=list)  # 実行されたフック名（例外時は "名前(error)" の形で記録）


HookFunc = Callable[[HookContext], HookVerdict | None]


@dataclass(frozen=True)
class _HookEntry:
    order: int
    seq: int
    name: str
    func: HookFunc


_REGISTRY: dict[str, list[_HookEntry]] = {}
_SEQ_COUNTER = itertools.count(1)


def _is_hooks_enabled() -> bool:
    """HOOKS_ENABLED 設定を参照する（import 失敗・例外時は True として扱う）。"""
    try:
        fn = cfg_bool
        if fn is _default_cfg_bool and _config_helpers is not None:
            fn = getattr(_config_helpers, "cfg_bool", fn)
        if fn is None:
            from ollama_bot.common.config_helpers import cfg_bool as imported_fn

            fn = imported_fn
        return bool(fn("HOOKS_ENABLED", True))
    except Exception:
        return True


# ============================================================================
# レジストリとランナー
# ============================================================================
def register_hook(point: str, name: str, func: HookFunc, *, order: int = 100) -> None:
    """指定された接点（point）にフック関数を登録する。"""
    entries = _REGISTRY.setdefault(point, [])
    entries[:] = [e for e in entries if e.name != name]
    entries.append(_HookEntry(order=int(order), seq=next(_SEQ_COUNTER), name=name, func=func))


def clear_hooks(point: str | None = None) -> None:
    """登録済みフックを消去する（テスト用）。point 指定時はその接点のみ消去する。"""
    if point is None:
        _REGISTRY.clear()
    else:
        _REGISTRY.pop(point, None)


def run_hooks(point: str, context: HookContext) -> HookOutcome:
    """指定された接点（point）のフック群を order 昇順（同順は登録順）で実行する。"""
    if not _is_hooks_enabled():
        return HookOutcome(blocked=False, reason="", text=context.text, applied=[])

    entries = _REGISTRY.get(point)
    if not entries:
        return HookOutcome(blocked=False, reason="", text=context.text, applied=[])

    sorted_entries = sorted(entries, key=lambda e: (e.order, e.seq))
    applied: list[str] = []

    for entry in sorted_entries:
        try:
            verdict = entry.func(context)
        except Exception as exc:
            logger.warning(
                "Hook '%s' at point '%s' raised an exception (fail-open): %s",
                entry.name,
                point,
                exc,
            )
            applied.append(f"{entry.name}(error)")
            continue

        if verdict is None or verdict.action == "allow":
            applied.append(entry.name)
            continue

        if verdict.action == "rewrite":
            if verdict.text is not None:
                context.text = verdict.text
            applied.append(entry.name)
            continue

        if verdict.action == "block":
            applied.append(entry.name)
            return HookOutcome(
                blocked=True,
                reason=verdict.reason,
                text=context.text,
                applied=applied,
            )

        logger.warning(
            "Hook '%s' at point '%s' returned unknown action '%s' (treated as allow)",
            entry.name,
            point,
            verdict.action,
        )
        applied.append(entry.name)

    return HookOutcome(
        blocked=False,
        reason="",
        text=context.text,
        applied=applied,
    )


__all__ = [
    "REPLY_PRE_SEND",
    "POST_PRE_SEND",
    "MEMORY_PRE_STORE",
    "HookContext",
    "HookVerdict",
    "HookOutcome",
    "HookFunc",
    "register_hook",
    "clear_hooks",
    "run_hooks",
]
