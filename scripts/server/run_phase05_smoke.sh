#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_phase05_smoke_${GIT_SHA}}"
RUN_DIR="${RUN_DIR:-runs/${RUN_ID}}"

if [[ -e "$RUN_DIR" ]]; then
  echo "Run directory already exists: $RUN_DIR" >&2
  exit 2
fi
mkdir -p "$RUN_DIR"

{
  echo "RUN_DIR=$RUN_DIR"
  echo "bash scripts/server/env_check.sh"
  echo "RUN_DIR=$RUN_DIR bash scripts/server/run_phase05_tests.sh"
} > "$RUN_DIR/commands.txt"

bash scripts/server/env_check.sh > "$RUN_DIR/env_check.txt" 2>&1
RUN_DIR="$RUN_DIR" bash scripts/server/run_phase05_tests.sh

echo "Smoke run complete: $RUN_DIR"
