#!/usr/bin/env bash
set -euo pipefail
if ! command -v pip-compile &> /dev/null; then
    echo "pip-tools is required. Run 'pip install pip-tools' first."
    exit 1
fi
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
