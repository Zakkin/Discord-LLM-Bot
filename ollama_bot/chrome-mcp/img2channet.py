"""img.2chan.net helper facade for Discord bot integrations.

このファイルは、img.2chan.net のスレッドURL検出、スレ本文取得・HTML解析、
投稿文の整形、引用つき返信の組み立て、Chrome MCP/HTTPによる投稿、勢い順
カタログ取得を外部へ提供する互換入口です。

実装本体は `img2channet_parts/` に機能別で分割しています。既存コードはこの
`img2channet.py` を importlib で直接読み込むため、分割後も同じ属性名で使える
よう、部品ファイルをこのモジュールの名前空間に読み込んでいます。
"""
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_PARTS_DIR = Path(__file__).resolve().with_name("img2channet_parts")
_PART_FILES = (
    "00_constants.py",
    "10_thread_fetch.py",
    "20_post_text.py",
    "30_submission.py",
    "40_catalog.py",
)


def _load_img2channet_part(filename: str) -> None:
    part_path = _PARTS_DIR / filename
    source = part_path.read_text(encoding="utf-8")
    exec(compile(source, str(part_path), "exec"), globals(), globals())


for _part_file in _PART_FILES:
    _load_img2channet_part(_part_file)


del _part_file
