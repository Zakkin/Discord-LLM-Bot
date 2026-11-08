"""*.2chan.net（ふたば☆ちゃんねる）用 PlatformAdapter 実装。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from front.bridge import default_reply_actions
from front.hutaba.utils import normalize_img_thread_url
from front.ir import Actor, AgentOutcome, AgentTask, FrontAction, FrontEvent, Location, Turn

import logging
log = logging.getLogger("front.hutaba")


class HutabaAdapter:
    """ふたば☆ちゃんねるのスレッドデータを `FrontEvent` へ正規化するアダプター。"""

    platform: str = "hutaba"

    def to_event(self, thread_data: Mapping[str, Any], *, kind: str = "thread_update") -> FrontEvent:
        url = normalize_img_thread_url(str(thread_data.get("url") or "")) or str(thread_data.get("url") or "")
        thread_no = str(thread_data.get("thread_no") or "")
        op_text = str(thread_data.get("op_text") or "").strip()
        replies = list(thread_data.get("replies") or [])
        turns = tuple(
            Turn(
                author=f"No.{item.get('no', '')}" if isinstance(item, dict) and item.get("no") else "としあき",
                text=str(item.get("text") or "").strip(),
            )
            for item in replies
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        )
        return FrontEvent(
            platform="hutaba",
            kind=kind,
            actor=Actor(id=thread_no, display_name="としあき"),
            location=Location(platform="hutaba", thread_id=thread_no, url=url),
            text=op_text,
            history=turns,
            metadata={"total_reply_count": int(thread_data.get("total_reply_count") or len(replies))},
        )

    def build_actions(self, task: AgentTask, outcome: AgentOutcome) -> list[FrontAction]:
        return default_reply_actions(task, outcome)

    async def execute(self, action: FrontAction) -> Any:
        if action.action != "post_reply":
            return {"status": "noop", "action": action.to_dict()}
        from front.hutaba.client import img2chan

        reply_text = str(action.payload.get("text") or "")
        log.info("[hutaba] HutabaAdapter レス送信実行: url=%s text=%r", action.target.url, reply_text)
        return await img2chan.submit_img_thread_reply(
            url=action.target.url,
            comment=reply_text,
            text=reply_text,
        )


__all__ = ["HutabaAdapter"]
