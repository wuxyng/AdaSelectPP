#!/usr/bin/env bash
set -euo pipefail
# Explicit, pre-registered per-window budgets. No CASES ratio column or algorithm mode.
: "${1:?Usage: scripts/adaselect.sh /absolute/registered-output-directory}"
python -m adasel.main --output "$1" campaign
