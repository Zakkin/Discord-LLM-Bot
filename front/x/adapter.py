"""x.com 用 PlatformAdapter 実装。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from front.bridge import default_reply_actions
from front.ir import Actor, AgentOutcome, AgentTask, FrontAction, FrontEvent, Location
from front.x.parser import X_TIMELINE_HOME_URL


class XAdapter:
    """X（x.com）の投稿・タイムライン項目を `FrontEvent` へ正規化するアダプター。"""

    platform: str = "x"

    def to_event(self, entry: Mapping[str, Any], *, kind: str = "timeline") -> FrontEvent:
        handle = str(entry.get("handle") or "").strip()
        author = str(entry.get("author") or handle).strip()
        text = str(entry.get("text") or "").strip()
        url = str(entry.get("url") or X_TIMELINE_HOME_URL).strip()
        return FrontEvent(
            platform="x",
            kind=kind,
            actor=Actor(id=handle, display_name=author, handle=handle),
            location=Location(platform="x", url=url),
            text=text,
            metadata={"raw": str(entry.get("raw") or "")},
        )

    def build_actions(self, task: AgentTask, outcome: AgentOutcome) -> list[FrontAction]:
        return default_reply_actions(task, outcome)

    async def execute(self, action: FrontAction) -> Any:
        return {"status": "noop", "action": action.to_dict()}


__all__ = ["XAdapter"]
