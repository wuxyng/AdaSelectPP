#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

LOG_FILE=""
if [[ -n "${RUN_DIR:-}" ]]; then
  mkdir -p "$RUN_DIR"
  LOG_FILE="$RUN_DIR/phase05_tests.log"
fi

run_tests() {
  printf '== compileall ==\n'
  python3 -m compileall -q adasel adaselect_pp util tests
  printf '== pytest PR0/PR1/PR2 invariants ==\n'
  python3 -m pytest -q \
    tests/test_phase05_pr0_invariants.py \
    tests/test_phase05_pr1_scale_selection.py \
    tests/test_phase05_pr2_probe_grow.py
}

if [[ -n "$LOG_FILE" ]]; then
  run_tests 2>&1 | tee "$LOG_FILE"
else
  run_tests
fi
