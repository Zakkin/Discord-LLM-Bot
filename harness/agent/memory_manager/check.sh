#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/../../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for memory_manager ==="
mypy --config-file mypy.ini harness/agent/memory_manager

echo "=== [2/2] Running pytest for memory_manager ==="
pytest tests/test_memory_logic.py tests/test_memory_store.py tests/test_memory_four_steps_verification.py tests/test_prospective_memory.py tests/test_working_memory.py
echo "✅ memory_manager checks passed!"
