"""§171: 周辺レス画像キャプション候補の引用先優先並べ替え（純粋関数群）。"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

T = TypeVar("T")

def order_context_image_candidates(
    candidates: Sequence[T] | Iterable[T],
    quoted_post_nos: Sequence[str] | Iterable[str],
    *,
    key_fn: Callable[[T], str],
) -> list[T]:
    """周辺画像キャプションの候補順を決める。

    quoted_post_nos（対象レスの引用先、引用順）に含まれる候補を先頭へ寄せ
    （引用順を優先）、残りは元の順（新しい順）のまま返す。
    key_fn(candidate) -> str で候補の投稿番号キーを得る。キーで重複を除く。
    """
    deduped: list[tuple[str, T]] = []
    seen_keys: set[str] = set()
    for cand in candidates:
        key = str(key_fn(cand) or "").strip()
        if key:
            if key in seen_keys:
                continue
            seen_keys.add(key)
        deduped.append((key, cand))

    by_key: dict[str, T] = {key: cand for key, cand in deduped if key}

    prioritized: list[T] = []
    picked_keys: set[str] = set()
    for raw_q in quoted_post_nos:
        q_key = str(raw_q or "").strip()
        if not q_key or q_key in picked_keys:
            continue
        if q_key in by_key:
            prioritized.append(by_key[q_key])
            picked_keys.add(q_key)

    rest: list[T] = [
        cand for key, cand in deduped if not key or key not in picked_keys
    ]
    return prioritized + rest


def is_quoted_image_priority_enabled() -> bool:
    """HUTABA_QUOTED_IMAGE_PRIORITY_ENABLED（既定 True）を読む。取得失敗時は True。"""
    try:
        from ollama_bot.common.config_helpers import cfg_bool

        return bool(cfg_bool("HUTABA_QUOTED_IMAGE_PRIORITY_ENABLED", True))
    except Exception:
        return True
