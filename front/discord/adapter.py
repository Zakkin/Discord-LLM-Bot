"""Discord 用 PlatformAdapter 実装。

`discord.Message` 互換オブジェクトを中間表現 `FrontEvent` / `AgentTask` へ正規化し、
`AgentBridge` 経由で `AgentOrchestrator` へ渡す橋渡しを担います。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from front.bridge import default_reply_actions
from front.discord.common.message_media import extract_first_image_url
from front.ir import Actor, AgentOutcome, AgentTask, FrontAction, FrontEvent, Location, Reference


def _is_channel_nsfw(channel: Any) -> bool:
    """Discord チャンネルが NSFW（年齢制限）であるかを安全に判定する。"""
    if channel is None:
        return False
    checker = getattr(channel, "is_nsfw", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    if hasattr(channel, "nsfw"):
        return bool(getattr(channel, "nsfw", False))
    return False


class DiscordAdapter:
    """Discord メッセージを `FrontEvent` / `AgentTask` へ変換するアダプター。"""

    platform: str = "discord"

    def message_to_event(
        self,
        message: Any,
        *,
        target_name: str = "",
        latest_ref: Any = None,
        ref_author: str = "",
        kind: str = "message",
    ) -> FrontEvent:
        author = getattr(message, "author", None)
        uid = getattr(author, "id", None)
        channel = getattr(message, "channel", None)
        cid = getattr(channel, "id", None)
        guild = getattr(message, "guild", None)
        gid = getattr(guild, "id", None)
        ref: Reference | None = None
        if latest_ref is not None:
            ref = Reference(
                message_id=str(getattr(latest_ref, "id", "") or ""),
                author=Actor(display_name=ref_author),
                text=str(getattr(latest_ref, "content", "") or ""),
            )

        is_nsfw = _is_channel_nsfw(channel)
        location_kwargs: dict[str, Any] = {
            "platform": "discord",
            "channel_id": str(cid) if cid is not None else "",
            "guild_id": str(gid) if gid is not None else "",
        }
        try:
            loc = Location(**{**location_kwargs, "is_nsfw": is_nsfw})
        except TypeError:
            loc = Location(**location_kwargs)

        return FrontEvent(
            platform="discord",
            kind=kind,
            actor=Actor(
                id=str(uid) if uid is not None else "",
                display_name=target_name or str(getattr(author, "display_name", "") or ""),
                is_bot=bool(getattr(author, "bot", False)),
            ),
            location=loc,
            text=str(getattr(message, "content", "") or ""),
            reply_to=ref,
            event_id=str(getattr(message, "id", "") or "") or FrontEvent(
                platform="discord",
                kind=kind,
                actor=Actor(),
                location=Location(platform="discord"),
            ).event_id,
        )

    def build_reply_task(
        self,
        message: Any,
        *,
        user_prompt: str,
        system_prompt: str,
        target_name: str,
        other_names: set[str],
        channel_id: Any,
        user_id: Any,
        latest_ref: Any = None,
        ref_author: str = "",
        image_url: str | None = None,
    ) -> AgentTask:
        event = self.message_to_event(
            message,
            target_name=target_name,
            latest_ref=latest_ref,
            ref_author=ref_author,
        )
        is_nsfw = _is_channel_nsfw(getattr(message, "channel", None))
        extra: dict[str, Any] = {
            "target_name": target_name,
            "other_names": other_names,
            "channel_id": channel_id,
            "user_id": user_id,
            "is_nsfw": is_nsfw,
        }
        target_image_url = (
            image_url
            or extract_first_image_url(message)
            or extract_first_image_url(latest_ref)
        )
        if target_image_url:
            extra["image_url"] = target_image_url

        return AgentTask(
            intent="reply",
            event=event,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            extra_context=extra,
        )

    def build_actions(self, task: AgentTask, outcome: AgentOutcome) -> list[FrontAction]:
        return default_reply_actions(task, outcome)

    async def execute(self, action: FrontAction) -> Any:
        return {"status": "delegated", "action": action.to_dict()}


__all__ = ["DiscordAdapter"]
