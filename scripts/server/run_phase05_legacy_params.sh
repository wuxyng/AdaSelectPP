#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
DRY_RUN="${DRY_RUN:-0}"
STRICT_RANGE="${STRICT_RANGE:-0}"
CASE_FILTER="${CASE_FILTER:-}"
TRACE="${TRACE:-1}"

GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
GIT_FULL_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
GIT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)_phase05_legacy_params_${GIT_SHA}}"
RUN_DIR="${RUN_DIR:-runs/${RUN_ID}}"

if [[ -e "$RUN_DIR" ]]; then
  echo "Run directory already exists: $RUN_DIR" >&2
  exit 2
fi

mkdir -p "$RUN_DIR"

printf '%s\n' "$GIT_FULL_SHA" > "$RUN_DIR/git_sha.txt"
printf '%s\n' "$GIT_BRANCH" > "$RUN_DIR/git_branch.txt"
git status --short > "$RUN_DIR/git_status.txt" 2>/dev/null || true
"$PYTHON_BIN" -m json.tool adasel/config/adaselect.json > "$RUN_DIR/config_dump.json"

trace_args=()
if [[ "$TRACE" == "1" ]]; then
  trace_args+=(--trace)
fi

range_to_list() {
  local rng="$1"
  local start="${rng%-*}"
  local end="${rng#*-}"
  local out="list:"
  local i

  if [[ ! "$start" =~ ^[0-9]+$ || ! "$end" =~ ^[0-9]+$ || "$start" -gt "$end" ]]; then
    echo "Invalid legacy range: $rng" >&2
    exit 2
  fi

  for ((i=start; i<=end; i++)); do
    out+="${i},"
  done
  echo "${out%,}"
}

actual_invoke_for() {
  local legacy_range="$1"
  if [[ "$STRICT_RANGE" == "1" ]]; then
    range_to_list "$legacy_range"
  else
    echo "all"
  fi
}

case_matches_filter() {
  local bench="$1"
  local wtype="$2"
  local filt="${CASE_FILTER:-}"
  local token
  local normalized

  [[ -z "$filt" ]] && return 0
  normalized="${filt//,/ }"
  for token in $normalized; do
    [[ -z "$token" ]] && continue
    if [[ "$token" == "${bench}:${wtype}" || "$token" == "${bench}_${wtype}" || "$token" == "${bench} ${wtype}" ]]; then
      return 0
    fi
  done
  return 1
}

write_case_metadata() {
  local path="$1"
  local bench="$2"
  local wtype="$3"
  local round_size="$4"
  local legacy_range="$5"
  local actual_invoke="$6"
  local alpha="$7"
  local beta="$8"
  local op="$9"

  cat > "$path" <<EOF
benchmark=$bench
workload_type=$wtype
round_size=$round_size
legacy_range=$legacy_range
actual_invoke=$actual_invoke
alpha=$alpha
beta=$beta
op=$op
lambda_policy=adaptive
wdcg_enabled=1
EOF
}

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
  local legacy_range="$4"
  local alpha="$5"
  local beta="$6"
  local op="$7"
  local actual_invoke
  local label="${bench}_${wtype}"
  local case_dir="$RUN_DIR/$label"

  actual_invoke="$(actual_invoke_for "$legacy_range")"
  mkdir -p "$case_dir"
  write_case_metadata "$case_dir/metadata.env" "$bench" "$wtype" "$round_size" "$legacy_range" "$actual_invoke" "$alpha" "$beta" "$op"

  local cmd=(
    "$PYTHON_BIN" adasel/main.py adaselect "$bench" "$wtype" "$round_size" "$actual_invoke" optimizer
    --alpha "$alpha"
    --beta "$beta"
    --opratio "$op"
    --lambda_policy adaptive
    --wdcg_enabled 1
  )
  cmd+=("${trace_args[@]}")

  printf '== %s round_size=%s legacy_range=%s actual_invoke=%s alpha=%s beta=%s op=%s ==\n' \
    "$label" "$round_size" "$legacy_range" "$actual_invoke" "$alpha" "$beta" "$op"
  printf '%s ' "${cmd[@]}" > "$case_dir/command.txt"
  printf '\n' >> "$case_dir/command.txt"
  cat "$case_dir/command.txt" >> "$RUN_DIR/commands.txt"

  if [[ "$DRY_RUN" == "1" ]]; then
    printf '[DRY_RUN] would run: ' | tee "$case_dir/stdout.log"
    cat "$case_dir/command.txt" | tee -a "$case_dir/stdout.log"
    : > "$case_dir/stderr.log"
    return 0
  fi

  archive_existing_case_outputs "$bench" "$wtype"

  if ! PYTHONUNBUFFERED=1 "${cmd[@]}" > "$case_dir/stdout.log" 2> "$case_dir/stderr.log"; then
    copy_case_outputs "$bench" "$wtype" "$case_dir"
    echo "Case failed: $label; see $case_dir/stdout.log and $case_dir/stderr.log" >&2
    exit 1
  fi

  copy_case_outputs "$bench" "$wtype" "$case_dir"
  verify_case_outputs "$case_dir" "$label"
}

write_dry_run_summary() {
  local out="$RUN_DIR/summary.md"
  {
    echo "# Phase 0.5 Legacy-Parameter Dry Run"
    echo
    echo "- run_dir: \`$RUN_DIR\`"
    echo "- strict_range: $STRICT_RANGE"
    echo "- case_filter: ${CASE_FILTER:-all}"
    echo
    echo "## Planned Cases"
    echo
    local meta
    for meta in "$RUN_DIR"/*/metadata.env; do
      [[ -f "$meta" ]] || continue
      echo "### $(basename "$(dirname "$meta")")"
      sed 's/^/- /' "$meta"
      echo
    done
  } > "$out"
  echo "$out"
}

cat > "$RUN_DIR/commands.txt" <<EOF
git_sha=$GIT_FULL_SHA
git_branch=$GIT_BRANCH
DRY_RUN=$DRY_RUN
STRICT_RANGE=$STRICT_RANGE
CASE_FILTER=${CASE_FILTER}
PYTHON_BIN=$PYTHON_BIN
bash scripts/server/env_check.sh
EOF

echo "Run directory: $RUN_DIR"
if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY_RUN=1: skipping env_check and experiment execution" | tee "$RUN_DIR/env_check.txt"
else
  if ! bash scripts/server/env_check.sh > "$RUN_DIR/env_check.txt" 2>&1; then
    cat "$RUN_DIR/env_check.txt"
    echo "Environment check failed; see $RUN_DIR/env_check.txt" >&2
    exit 1
  fi
fi

CASES=(
  "tpch shifting 5 0-78 0.55 1.1 0.5"
  "tpch noisy 5 0-94 0.70 1.1 0.5"
  "tpch random 21 0-23 0.50 0.8 0.5"
  "tpchs shifting 5 0-78 0.85 1.1 0.5"
  "tpchs noisy 5 0-94 0.60 0.9 0.5"
  "tpchs random 21 0-23 0.35 1.1 0.5"
  "job shifting 8 0-78 0.90 1.1 0.25"
  "job noisy 8 0-94 0.70 1.5 0.25"
  "job random 33 0-23 0.80 1.5 0.25"
)

matched=0
for line in "${CASES[@]}"; do
  read -r bench wtype round_size legacy_range alpha beta op <<< "$line"
  if ! case_matches_filter "$bench" "$wtype"; then
    continue
  fi
  run_case "$bench" "$wtype" "$round_size" "$legacy_range" "$alpha" "$beta" "$op"
  matched=1
done

if [[ "$matched" -eq 0 ]]; then
  echo "No case matched CASE_FILTER='${CASE_FILTER}'" >&2
  exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  write_dry_run_summary
else
  "$PYTHON_BIN" scripts/server/summarize_phase05.py "$RUN_DIR"
fi

echo "Legacy-parameter run complete: $RUN_DIR"
