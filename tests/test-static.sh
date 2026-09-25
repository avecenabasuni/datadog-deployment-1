#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$TASK_ROOT"
for entry in scripts/datadog-bootstrap scripts/compose scripts/eminerba-production scripts/eminerba-lab; do
  bash -n "$entry"
done
python3 -m unittest discover -s tests -p 'test_*.py' -v
