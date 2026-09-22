#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$TASK_ROOT"
bash -n scripts/datadog-bootstrap
python3 -m unittest discover -s tests -p 'test_*.py' -v
