#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/../../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for orchestrator ==="
mypy --config-file mypy.ini harness/agent/orchestrator

echo "=== [2/2] Running pytest for orchestrator ==="
pytest harness/agent/orchestrator/tests/ tests/test_reflection_prompt.py
echo "✅ orchestrator checks passed!"
