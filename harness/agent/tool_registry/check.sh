#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/../../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs:${PYTHONPATH}"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for tool_registry ==="
mypy --config-file mypy.ini harness/agent/tool_registry

echo "=== [2/2] Running pytest for tool_registry ==="
pytest tests/test_harness_tool_registry.py tests/test_harness_mcp.py tests/test_weather_helpers.py tests/test_umigame_helpers.py harness/agent/tool_registry/tests/test_special_action_and_helpers.py
echo "✅ tool_registry checks passed!"
