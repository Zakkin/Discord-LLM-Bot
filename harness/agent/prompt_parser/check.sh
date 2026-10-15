#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/../../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs:${PYTHONPATH}"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for prompt_parser ==="
mypy --config-file mypy.ini harness/agent/prompt_parser

echo "=== [2/2] Running pytest for prompt_parser ==="
pytest tests/test_harness_prompt_parser.py tests/test_chat_prompt_logic.py tests/test_reply_helpers*.py tests/test_think_salvage.py
echo "✅ prompt_parser checks passed!"
