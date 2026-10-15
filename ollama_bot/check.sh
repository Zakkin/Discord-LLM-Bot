#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for ollama_bot ==="
mypy --config-file mypy.ini ollama_bot

echo "=== [2/2] Running pytest for ollama_bot ==="
pytest tests/test_ollama_chat_lifecycle.py tests/test_bot_lifecycle_commands.py tests/test_interaction_guard.py tests/test_unknown_reply_skip.py
echo "✅ ollama_bot checks passed!"
