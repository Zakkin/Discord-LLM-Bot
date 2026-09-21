from __future__ import annotations

import json
from typing import Any

JSONDecodeError: type[Exception]

try:
    import orjson
except ImportError:
    orjson = None
    JSONDecodeError = json.JSONDecodeError
else:
    JSONDecodeError = orjson.JSONDecodeError


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    if orjson is not None:
        return orjson.loads(data)

    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")
    return json.loads(data)


def dumps(value: Any, *, ensure_ascii: bool = False) -> str:
    if orjson is not None and not ensure_ascii:
        return orjson.dumps(value).decode("utf-8")
    return json.dumps(value, ensure_ascii=ensure_ascii)


def aiohttp_dumps(value: Any) -> str:
    return dumps(value, ensure_ascii=False)
