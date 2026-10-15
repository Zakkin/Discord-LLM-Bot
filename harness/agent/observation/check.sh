#!/usr/bin/env bash
set -e
SUBDIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "${SUBDIR}/../../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}:${ROOT_DIR}/mock_libs"
cd "${ROOT_DIR}"

echo "=== [1/2] Running mypy for observation ==="
mypy --config-file mypy.ini harness/agent/observation

echo "=== [2/2] Running pytest for observation ==="
pytest harness/agent/observation/tests/
echo "✅ observation checks passed!"
