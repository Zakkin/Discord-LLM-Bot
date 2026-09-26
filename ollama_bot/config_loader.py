# ollama_bot/config_loader.py
from __future__ import annotations

import importlib
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

EXCLUDED_CONFIG_MODULES: frozenset[str] = frozenset({
    "config_base",
    "config_sample",
    "config_loader",
    "config_test",
})


def find_available_config_modules(target_dir: str | None = None) -> list[str]:
    """base/sample/loader/test等を除外した実在する config_*.py のモジュール名一覧を取得する。"""
    if target_dir is None:
        target_dir = os.path.dirname(__file__)

    if not os.path.isdir(target_dir):
        return []

    candidates: list[str] = []
    for fname in sorted(os.listdir(target_dir)):
        if fname.startswith("config_") and fname.endswith(".py"):
            mod_name = fname[:-3]
            if mod_name not in EXCLUDED_CONFIG_MODULES:
                candidates.append(mod_name)
    return candidates


here = os.path.dirname(__file__)
CONFIG_MODULE: str = os.environ.get("OLLAMA_BOT_CONFIG", "").strip()

# 明示的に指定されている場合は正規化と存在チェックを行う
if CONFIG_MODULE:
    raw_mod = CONFIG_MODULE.split(".")[-1]
    if raw_mod.endswith(".py"):
        raw_mod = raw_mod[:-3]
    if not os.path.exists(os.path.join(here, f"{raw_mod}.py")) and os.path.exists(os.path.join(here, f"config_{raw_mod}.py")):
        raw_mod = f"config_{raw_mod}"
    CONFIG_MODULE = raw_mod

    if CONFIG_MODULE in sys.modules or f"{__package__}.{CONFIG_MODULE}" in sys.modules:
        pass
    else:
        candidate_file = os.path.join(here, f"{CONFIG_MODULE}.py")
        if not os.path.exists(candidate_file):
            logger.warning(
                "Config module '%s' (%s) does not exist. Falling back to auto-detected config.",
                CONFIG_MODULE,
                candidate_file,
            )
            CONFIG_MODULE = ""

# 未指定または指定されたファイルが存在しなかった場合、自動検出を行う
if not CONFIG_MODULE:
    available = find_available_config_modules(here)
    if available:
        CONFIG_MODULE = available[0]
    elif os.path.exists(os.path.join(here, "config_sample.py")):
        CONFIG_MODULE = "config_sample"
    else:
        raise FileNotFoundError(
            f"No valid config module found in {here} (excluding {set(EXCLUDED_CONFIG_MODULES)})."
        )

if "." in CONFIG_MODULE or CONFIG_MODULE in sys.modules:
    module_name = CONFIG_MODULE
else:
    module_name = f"{__package__}.{CONFIG_MODULE}"

if module_name in sys.modules:
    config: Any = sys.modules[module_name]
else:
    config = importlib.import_module(module_name)
