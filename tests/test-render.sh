#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$TASK_ROOT"
# Retained entry point: renderer, escaping, dry-run and rerun use unittest mocks.
python3 -m unittest discover -s tests -p 'test_*.py' -v
