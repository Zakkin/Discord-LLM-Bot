"""後方互換性のためのchrome_mcp_serverフォワーダー。実装は harness.agent.tool_registry.mcp.chrome.server に移行しました。"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import harness.agent.tool_registry.mcp.chrome.server as _mod
from harness.agent.tool_registry.mcp.chrome.server import *  # noqa: F401, F403

if __name__ == "__main__":
    import uvicorn
    from harness.agent.tool_registry.mcp.chrome.server import app, logger
    logger.info("Starting Chrome MCP Proxy Server on port 12306...")
    uvicorn.run(app, host="127.0.0.1", port=12306)
