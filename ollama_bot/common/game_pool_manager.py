import asyncio
import logging
from pathlib import Path
from typing import Any

from lib.json_utils import load_json, save_json_atomic_async

log = logging.getLogger("ollama_bot.common.game_pool_manager")

class GamePoolManager:
    def __init__(self, storage_path: str) -> None:
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        
        self.umigame_pool: list[dict[str, Any]] = []
        self.twenty_doors_pool: list[dict[str, Any]] = []
        
        self._load()

    def _load(self) -> None:
        try:
            data = load_json(self.storage_path, default={})
            if isinstance(data, dict):
                self.umigame_pool = data.get("umigame_pool", [])
                self.twenty_doors_pool = data.get("twenty_doors_pool", [])
        except Exception as e:
            log.warning("failed to load game pool: %r", e)

    async def _save(self) -> None:
        try:
            data = {
                "umigame_pool": self.umigame_pool,
                "twenty_doors_pool": self.twenty_doors_pool,
            }
            await save_json_atomic_async(self.storage_path, data)
        except Exception as e:
            log.warning("failed to save game pool: %r", e)

    async def pop_umigame(self) -> dict[str, Any] | None:
        async with self._lock:
            if not self.umigame_pool:
                return None
            item = self.umigame_pool.pop(0)
            await self._save()
            return item

    async def push_umigame(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self.umigame_pool.append(item)
            await self._save()

    async def pop_twenty_doors(self) -> dict[str, Any] | None:
        async with self._lock:
            if not self.twenty_doors_pool:
                return None
            item = self.twenty_doors_pool.pop(0)
            await self._save()
            return item

    async def push_twenty_doors(self, item: dict[str, Any]) -> None:
        async with self._lock:
            self.twenty_doors_pool.append(item)
            await self._save()

    def get_counts(self) -> dict[str, int]:
        return {
            "umigame": len(self.umigame_pool),
            "twenty_doors": len(self.twenty_doors_pool),
        }
