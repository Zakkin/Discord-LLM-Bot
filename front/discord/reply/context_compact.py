"""古い会話ログを要約・マーカーに畳む純粋関数"""
from __future__ import annotations


def compact_context_lines(
    lines: list[str],
    *,
    trigger_lines: int = 40,
    keep_recent: int = 12,
    summary_lines: list[str] | None = None,
) -> list[str]:
    if len(lines) <= trigger_lines:
        return list(lines)
    keep = max(1, keep_recent)
    recent = lines[-keep:]
    marker = "（これより前のやり取りは要約に圧縮されています）"
    compacted: list[str] = [marker]
    if summary_lines:
        compacted.extend(summary_lines)
    compacted.extend(recent)
    return compacted
