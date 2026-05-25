#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ "$#" -ne 1 ]]; then
  echo "Usage: bash scripts/server/pack_results.sh <run_dir>" >&2
  exit 2
fi

RUN_DIR="${1%/}"
if [[ ! -d "$RUN_DIR" ]]; then
  echo "Run directory not found: $RUN_DIR" >&2
  exit 1
fi

RUN_NAME="$(basename "$RUN_DIR")"
RUN_PARENT="$(dirname "$RUN_DIR")"
ARTIFACT_DIR="artifacts"
ARTIFACT_PATH="$ARTIFACT_DIR/${RUN_NAME}.tar.gz"

mkdir -p "$ARTIFACT_DIR"
if [[ -e "$ARTIFACT_PATH" ]]; then
  echo "Artifact already exists: $ARTIFACT_PATH" >&2
  exit 2
fi

tar -czf "$ARTIFACT_PATH" -C "$RUN_PARENT" "$RUN_NAME"
echo "$ARTIFACT_PATH"
