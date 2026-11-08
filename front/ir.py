"""front 層の中間表現（Intermediate Representation）。

Discord / ふたば（*.2chan.net）/ X（x.com）など、各プラットフォーム固有の入力を
プラットフォーム非依存の構造へ正規化し、`harness/agent/`（AgentOrchestrator 等）へ
受け渡すための「中間コード」を定義します。

データの流れ::

    プラットフォーム固有オブジェクト
        └─(front/<platform>/adapter.py)→ FrontEvent
            └─(front/bridge.py)→ AgentTask ──→ AgentOrchestrator.run(...)
                                      AgentOutcome ←──┘
            └─(front/bridge.py / adapter)→ FrontAction → プラットフォームへ出力

このモジュールは標準ライブラリのみに依存し、`discord` や `harness` を import しません。
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal

Platform = Literal["discord", "hutaba", "x"]
"""front 層がサポートするプラットフォーム識別子。"""

SUPPORTED_PLATFORMS: tuple[str, ...] = ("discord", "hutaba", "x")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def to_jsonable(value: Any) -> Any:
    """dataclass / set / tuple / Mapping を JSON シリアライズ可能な値へ再帰変換する。"""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((to_jsonable(v) for v in value), key=lambda v: str(v))
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass(frozen=True)
class Actor:
    """発言者・投稿者の正規化表現。"""

    id: str = ""
    display_name: str = ""
    is_bot: bool = False
    handle: str = ""


@dataclass(frozen=True)
class Location:
    """発言・投稿の場所（チャンネル / スレッド / URL）の正規化表現。"""

    platform: str
    channel_id: str = ""
    thread_id: str = ""
    guild_id: str = ""
    url: str = ""
    is_nsfw: bool = False


@dataclass(frozen=True)
class Attachment:
    """添付ファイル（画像など）の正規化表現。"""

    kind: str = "file"
    url: str = ""
    filename: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class Reference:
    """返信先・引用先の正規化表現。"""

    message_id: str = ""
    author: Actor | None = None
    text: str = ""


@dataclass(frozen=True)
class Turn:
    """会話履歴の1発言。"""

    author: str
    text: str
    timestamp: float | None = None


@dataclass(frozen=True)
class FrontEvent:
    """プラットフォーム → エージェント方向の入力イベント。"""

    platform: str
    kind: str
    actor: Actor
    location: Location
    text: str = ""
    attachments: tuple[Attachment, ...] = ()
    reply_to: Reference | None = None
    history: tuple[Turn, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_new_id)
    created_at: float = field(default_factory=time.time)
    is_nsfw: bool = False

    def __post_init__(self) -> None:
        if self.platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {self.platform!r}")

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class Constraints:
    """エージェント出力に課す制約（プラットフォームごとの投稿ルール）。"""

    max_chars: int | None = None
    allow_post: bool = True
    forbidden_phrases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentTask:
    """bridge が生成し AgentOrchestrator へ渡す「中間コード」。

    `orchestrator_context()` は `AgentOrchestrator.run(context=...)` にそのまま渡せる
    辞書を返します。`extra_context` に与えたキーはイベント由来の既定値より優先されます。
    """

    intent: str
    event: FrontEvent
    user_prompt: str
    system_prompt: str = ""
    constraints: Constraints = field(default_factory=Constraints)
    tool_hints: tuple[str, ...] = ()
    extra_context: Mapping[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=_new_id)

    def orchestrator_context(self) -> dict[str, Any]:
        is_nsfw_val = bool(
            getattr(self.event, "is_nsfw", False)
            or getattr(self.event.location, "is_nsfw", False)
        )
        ctx: dict[str, Any] = {
            "platform": self.event.platform,
            "target_name": self.event.actor.display_name,
            "channel_id": self.event.location.channel_id or None,
            "user_id": self.event.actor.id or None,
            "is_nsfw": is_nsfw_val,
        }
        ctx.update(dict(self.extra_context))
        return ctx

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class AgentOutcome:
    """AgentOrchestrator の実行結果を front 層向けに正規化したもの。"""

    task_id: str
    success: bool
    text: str = ""
    error: str | None = None
    total_turns: int = 0
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class FrontAction:
    """エージェント → プラットフォーム方向の出力アクション。"""

    platform: str
    action: str
    target: Location
    payload: Mapping[str, Any] = field(default_factory=dict)
    reply_to: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


__all__ = [
    "Actor",
    "AgentOutcome",
    "AgentTask",
    "Attachment",
    "Constraints",
    "FrontAction",
    "FrontEvent",
    "Location",
    "Platform",
    "Reference",
    "SUPPORTED_PLATFORMS",
    "Turn",
    "to_jsonable",
]
