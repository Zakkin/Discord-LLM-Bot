"""ユーザープロンプトのセクション計測と段階的予算トリミング処理。"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..system_prompts import USER_REPLY_BASE_PARTS

log = logging.getLogger("harness.agent.prompt_manager.builder")

_PROTECTED_SECTION_HEADERS: tuple[str, ...] = (
    "【直前の誤りの訂正指示（最重要）】",
    "【否定済み主張の再主張禁止リスト】",
    "【直近の出来事メモ（行為者と行動）】",
    "【現在の会話状態メモ】",
    "【思い出し: あなたの過去の発言について聞かれています】",
    "【記憶の出所: 知識や発言の出所・根拠について聞かれています】",
)

_KNOWN_SECTION_PREFIXES: tuple[str, ...] = (
    "最近の会話要約:",
    "会話履歴:",
    "直前のAI発言:",
    "直前の会話文脈（参考）:",
    "直近のチャンネル会話:",
    "追加指示:",
    "追加ルール:",
    "添付・リンクの補足情報:",
    "チャンネル要約:",
    "この相手の過去の記憶や参考情報:",
    "相手ごとの会話習慣プロファイル:",
)


def _extract_section_header(part: str) -> tuple[str | None, bool]:
    """part の先頭行からセクション見出しと、見出し行単体かどうかを返す。"""
    lines = str(part or "").splitlines()
    if not lines:
        return None, False
    first_line = lines[0].strip()
    if not first_line:
        return None, False
    if first_line.startswith("【"):
        end_idx = first_line.find("】")
        if end_idx >= 0:
            header = first_line[: end_idx + 1]
            rest = first_line[end_idx + 1 :].strip()
            return header, (len(lines) == 1 and not rest)
        return first_line, (len(lines) == 1)
    for prefix in _KNOWN_SECTION_PREFIXES:
        if first_line.startswith(prefix):
            rest = first_line[len(prefix) :].strip()
            return prefix, (len(lines) == 1 and not rest)
    return None, False


def _is_protected_section_header(part: str) -> bool:
    """指定された part が保護対象セクションの見出しであるかを判定する。"""
    raw = str(part or "").strip()
    if not raw:
        return False
    if raw in _PROTECTED_SECTION_HEADERS:
        return True
    first_line = raw.splitlines()[0].strip()
    if first_line in _PROTECTED_SECTION_HEADERS:
        return True
    header, _ = _extract_section_header(raw)
    return bool(header and header in _PROTECTED_SECTION_HEADERS)


def _measure_prompt_sections(parts: list[str]) -> list[tuple[str, int]]:
    """parts を見出し単位のセクションに分け、(見出し, 文字数) の列を返す（計測専用）。"""
    sections: list[list[Any]] = []
    active_idx: int | None = None

    for part in parts:
        raw = str(part or "")
        if not raw.strip():
            active_idx = None
            continue
        header, is_header_only = _extract_section_header(raw)
        if header is not None:
            sections.append([header, len(raw)])
            active_idx = (len(sections) - 1) if is_header_only else None
        elif active_idx is not None:
            sections[active_idx][1] += len(raw)
        else:
            sections.append(["(見出しなし)", len(raw)])

    return [(str(name), int(chars)) for name, chars in sections]


def _trim_user_prompt_parts_to_budget(
    parts: list[str],
    max_chars: int,
    protected_lines: Sequence[str] = (),
    *,
    user_reply_base_parts: Sequence[str] | None = None,
) -> list[str]:
    """ユーザープロンプト全体の文字数が上限を超える場合、優先度の低いセクションから段階的にトリミングする。

    優先度高（死守）: 対象メッセージ、現在の対話相手、返信関係、キャラ人格、推論/パズル指示、
                    訂正・否定された主張、会話状態メモ（recent_events含む）、思い出しブロック、記憶の出所ブロック
    優先度低（削減可）: 並行スレッド一覧 → 習慣プロファイル・関心事 → 会話要約 → 古い会話履歴 → ツールカタログ・長期記憶候補
    """
    current_raw = "\n".join(parts).strip()
    if len(current_raw) <= max_chars:
        return parts

    log.warning(
        "[prompt-manager] User prompt exceeded budget (%d > %d); applying graceful trimming",
        len(current_raw),
        max_chars,
    )
    _section_sizes = _measure_prompt_sections(parts)
    _top = sorted(_section_sizes, key=lambda kv: kv[1], reverse=True)[:3]
    log.warning(
        "[prompt-manager] prompt section sizes: total=%d sections=%d top=%s",
        len(current_raw),
        len(_section_sizes),
        ", ".join(f"{name}={chars}" for name, chars in _top),
    )

    trimmed_parts = list(parts)

    # 1. 並行スレッド一覧の削減
    _stage1_headers = (
        "【並行して進行中の別の話題（混同禁止）】",
        "【進行中の並行トピック（別スレッド）】",
        "【進行中のマルチトピック】",
    )
    new_parts: list[str] = []
    skip_section = False
    for p in trimmed_parts:
        if _is_protected_section_header(p):
            skip_section = False
            new_parts.append(p)
            continue
        p_header, p_header_only = _extract_section_header(p)
        if p in _stage1_headers or p_header in _stage1_headers:
            if p_header_only or "\n" not in p:
                skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※"))):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 2. 会話習慣プロファイルや関心事、出来事の記憶を削減
    _stage2_headers = (
        "【相手ごとの会話習慣プロファイル】",
        "【相手の関心事・興味】",
        "【関連する出来事の記憶】",
    )
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if _is_protected_section_header(p):
            skip_section = False
            new_parts.append(p)
            continue
        p_header, p_header_only = _extract_section_header(p)
        if p in _stage2_headers or p_header in _stage2_headers:
            if p_header_only or "\n" not in p:
                skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※"))):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 3. 会話要約を削減
    _stage3_headers = ("最近の会話要約:", "【チャンネル要約】")
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if _is_protected_section_header(p):
            skip_section = False
            new_parts.append(p)
            continue
        p_header, p_header_only = _extract_section_header(p)
        if p in _stage3_headers or p_header in _stage3_headers:
            if p_header_only or "\n" not in p:
                skip_section = True
            continue
        if skip_section and (p == "" or p.startswith("【") or p.startswith("直前のAI発言:") or p.startswith("会話履歴:")):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 3.5 システムプロンプトと重複する固定ルール文（USER_REPLY_BASE_PARTS）を1行に圧縮（会話履歴より先に削る）
    effective_base_parts = USER_REPLY_BASE_PARTS if user_reply_base_parts is None else user_reply_base_parts
    _base_keep = set(effective_base_parts[:2])
    _base_set = set(effective_base_parts)
    _compact_note = (
        "※返答時の禁止事項・口調ルールはシステムプロンプトの【絶対厳守ルール】1〜28に従ってください"
        "（オウム返し・キメラ合成・メタ無知表明・第三者解説・自分宛てと誤認した困惑は厳禁）。"
    )
    new_parts = []
    inserted_note = False
    for p in trimmed_parts:
        if not _is_protected_section_header(p) and p in _base_set and p not in _base_keep:
            if not inserted_note:
                new_parts.append(_compact_note)
                inserted_note = True
            continue
        new_parts.append(p)
    trimmed_parts = new_parts

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 4. 会話履歴が長い場合、直近3行のみに切り詰める（古い履歴から優先的にトリミング）
    has_history = False
    history_idx = -1
    for i, p in enumerate(trimmed_parts):
        if not _is_protected_section_header(p) and p == "会話履歴:":
            history_idx = i
            has_history = True
            break
    if has_history and history_idx >= 0:
        end_idx = history_idx + 1
        while (
            end_idx < len(trimmed_parts)
            and trimmed_parts[end_idx] != ""
            and not _is_protected_section_header(trimmed_parts[end_idx])
            and not trimmed_parts[end_idx].startswith("【")
        ):
            end_idx += 1
        history_lines = trimmed_parts[history_idx + 1:end_idx]
        if len(history_lines) > 3:
            protected = [str(x).strip() for x in protected_lines if str(x).strip()]
            tail = history_lines[-3:]
            kept_protected = [
                ln for ln in history_lines[:-3]
                if any(pl and (pl in ln or ln in pl) for pl in protected)
            ][-3:]
            trimmed_parts = (
                trimmed_parts[:history_idx + 1]
                + kept_protected
                + tail
                + trimmed_parts[end_idx:]
            )
    else:
        for i, p in enumerate(trimmed_parts):
            if not _is_protected_section_header(p) and p.startswith("会話履歴:\n"):
                lines = p.splitlines()
                history_lines = lines[1:]
                if len(history_lines) > 3:
                    protected = [str(x).strip() for x in protected_lines if str(x).strip()]
                    tail = history_lines[-3:]
                    kept_protected = [
                        ln for ln in history_lines[:-3]
                        if any(pl and (pl in ln or ln in pl) for pl in protected)
                    ][-3:]
                    trimmed_parts[i] = "\n".join([lines[0], *kept_protected, *tail])
                break

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 5. ツールカタログが長大な場合、概要のみにトリミング
    for i, p in enumerate(trimmed_parts):
        if (
            not _is_protected_section_header(p)
            and p == "【利用可能なツール（外部連携・MCP機能）】"
            and i + 1 < len(trimmed_parts)
            and not _is_protected_section_header(trimmed_parts[i + 1])
        ):
            if len(trimmed_parts[i + 1]) > 500:
                trimmed_parts[i + 1] = trimmed_parts[i + 1][:500] + "\n...(以降のツール定義は省略)"
            break
        if not _is_protected_section_header(p) and p.startswith("【利用可能なツール（外部連携・MCP機能）】\n"):
            header_line, body = p.split("\n", 1)
            if len(body) > 500:
                trimmed_parts[i] = header_line + "\n" + body[:500] + "\n...(以降のツール定義は省略)"
            break

    current_raw = "\n".join(trimmed_parts).strip()
    if len(current_raw) <= max_chars:
        return trimmed_parts

    # 6. それでも超過する場合、長期記憶候補を削減
    _stage6_headers = ("【この相手に関係すると確認できた記憶】",)
    new_parts = []
    skip_section = False
    for p in trimmed_parts:
        if _is_protected_section_header(p):
            skip_section = False
            new_parts.append(p)
            continue
        p_header, p_header_only = _extract_section_header(p)
        if p in _stage6_headers or p_header in _stage6_headers:
            if p_header_only or "\n" not in p:
                skip_section = True
            continue
        if skip_section and (p == "" or (p.startswith("【") and not p.startswith("※")) or p.startswith("直前のAI発言:")):
            skip_section = False
        if not skip_section:
            new_parts.append(p)
    trimmed_parts = new_parts

    return trimmed_parts
