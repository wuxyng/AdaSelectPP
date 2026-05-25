#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_FULL_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_phase05_firstpass_${GIT_SHA}}"
RUN_DIR="${RUN_DIR:-runs/${RUN_ID}}"

if [[ -e "$RUN_DIR" ]]; then
  echo "Run directory already exists: $RUN_DIR" >&2
  exit 2
fi

mkdir -p "$RUN_DIR"

printf '%s\n' "$GIT_FULL_SHA" > "$RUN_DIR/git_sha.txt"
printf '%s\n' "$GIT_BRANCH" > "$RUN_DIR/git_branch.txt"
git status --short > "$RUN_DIR/git_status.txt" || true
python3 -m json.tool adasel/config/adaselect.json > "$RUN_DIR/config_dump.json"

cat > "$RUN_DIR/commands.txt" <<EOF
git_sha=$GIT_FULL_SHA
git_branch=$GIT_BRANCH
bash scripts/server/env_check.sh
python3 adasel/main.py adaselect tpchs noisy 5 all optimizer --trace
python3 adasel/main.py adaselect tpchs random 21 all optimizer --trace
python3 adasel/main.py adaselect job random 33 all optimizer --trace
python3 scripts/server/summarize_phase05.py "$RUN_DIR"
EOF

echo "Run directory: $RUN_DIR"
if ! bash scripts/server/env_check.sh > "$RUN_DIR/env_check.txt" 2>&1; then
  cat "$RUN_DIR/env_check.txt"
  echo "Environment check failed; see $RUN_DIR/env_check.txt" >&2
  exit 1
fi

copy_case_outputs() {
  local bench="$1"
  local wtype="$2"
  local case_dir="$3"
  mkdir -p "$case_dir"
  if [[ -d log ]]; then
    while IFS= read -r -d '' f; do
      cp -p "$f" "$case_dir/"
    done < <(find log -maxdepth 1 -type f -name "adaselect_${bench}_${wtype}_*" -print0 2>/dev/null)
  fi
}

archive_existing_case_outputs() {
  local bench="$1"
  local wtype="$2"
  local archive_dir="$RUN_DIR/_preexisting_log_archive/${bench}_${wtype}"
  local files=()

  if [[ ! -d log ]]; then
    return 0
  fi

  shopt -s nullglob
  files=(log/adaselect_"${bench}"_"${wtype}"_*)
  shopt -u nullglob

  if (( ${#files[@]} == 0 )); then
    return 0
  fi

  mkdir -p "$archive_dir"
  printf 'Archiving %d preexisting log artifact(s) for %s_%s into %s\n' \
    "${#files[@]}" "$bench" "$wtype" "$archive_dir"
  mv -- "${files[@]}" "$archive_dir/"
}

verify_case_outputs() {
  local case_dir="$1"
  local label="$2"
  local metrics_count trace_count log_count

  metrics_count="$(find "$case_dir" -maxdepth 1 -type f -name '*.csv' ! -name '*.trace.csv' | wc -l | tr -d '[:space:]')"
  trace_count="$(find "$case_dir" -maxdepth 1 -type f -name '*.trace.csv' | wc -l | tr -d '[:space:]')"
  log_count="$(find "$case_dir" -maxdepth 1 -type f -name '*.log' | wc -l | tr -d '[:space:]')"

  if (( metrics_count < 1 || trace_count < 1 || log_count < 1 )); then
    echo "Missing expected artifacts for case $label in $case_dir" >&2
    echo "  metrics CSV (*.csv excluding *.trace.csv): $metrics_count" >&2
    echo "  trace CSV (*.trace.csv): $trace_count" >&2
    echo "  log file (*.log): $log_count" >&2
    echo "Collected files:" >&2
    find "$case_dir" -maxdepth 1 -type f -printf '  %f\n' >&2 2>/dev/null || true
    exit 1
  fi
}

run_case() {
  local bench="$1"
  local wtype="$2"
  local round_size="$3"
  local label="${bench}_${wtype}"
  local case_dir="$RUN_DIR/$label"
  mkdir -p "$case_dir"

  echo "== Running $label round_size=$round_size =="
  {
    echo "python3 adasel/main.py adaselect $bench $wtype $round_size all optimizer --trace"
  } > "$case_dir/command.txt"

  archive_existing_case_outputs "$bench" "$wtype"

  if ! python3 adasel/main.py adaselect "$bench" "$wtype" "$round_size" all optimizer --trace \
      > "$case_dir/stdout.log" 2> "$case_dir/stderr.log"; then
    copy_case_outputs "$bench" "$wtype" "$case_dir"
    echo "Case failed: $label; see $case_dir/stdout.log and $case_dir/stderr.log" >&2
    exit 1
  fi

  copy_case_outputs "$bench" "$wtype" "$case_dir"
  verify_case_outputs "$case_dir" "$label"
}

run_case tpchs noisy 5
run_case tpchs random 21
run_case job random 33

python3 scripts/server/summarize_phase05.py "$RUN_DIR"

echo "First-pass run complete: $RUN_DIR"
