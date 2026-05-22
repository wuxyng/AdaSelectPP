#!/usr/bin/env bash
# scripts/liteselectmc_topk.sh (NO-JSON, global width)
set -Eeuo pipefail
IFS=$'\n\t'

cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 127; }; }
need python

MAIN_PY="litesel/main.py"
ALGO="${ALGO:-LiteSelectMC_topk}"

# Optional debug mode:
#   - pass as first arg:   ./scripts/liteselectmc_topk.sh --debug
#   - or via env var:      DEBUG=1 ./scripts/liteselectmc_topk.sh
DEBUG_FLAG=""
if [[ "${1:-}" == "--debug" ]]; then
  DEBUG_FLAG="--debug"
  shift
elif [[ "${DEBUG:-0}" == "1" ]]; then
  DEBUG_FLAG="--debug"
fi

# ---- global knobs (same for all CASES) ----
MIN_WIDTH="${MIN_WIDTH:-1}"
MAX_WIDTH="${MAX_WIDTH:-3}"
# Optional: global timeout/rsfe_decay if you want
# TIMEOUT="${TIMEOUT:-}"
# RSFE_DECAY="${RSFE_DECAY:-}"

# "0-78" -> "[0,1,...,78]" ; if already "[...]" return as-is
gen_invoke() {
  local rng="$1"
  [[ "$rng" == \[*\] ]] && { echo "$rng"; return; }
  local a="${rng%-*}" b="${rng#*-}" out="[" i
  for ((i=a;i<=b;i++)); do out+="${i},"; done
  echo "${out%,}]"
}

# Pick a supported flag name by scanning --help
pick_flag() {
  local main_py="$1" a="$2" b="$3"
  local help
  help="$(python -u "$main_py" --help 2>&1 || true)"
  echo "$help" | grep -q -- "$a" && { echo "$a"; return; }
  echo "$help" | grep -q -- "$b" && { echo "$b"; return; }
  echo ""
}

# Resolve flags (support underscore or hyphen variants)
ALPHA_FLAG="--alpha"
BETA_FLAG="--beta"

OPR_FLAG="$(pick_flag "$MAIN_PY" "--opratio" "--optimizer_ratio")"
if [[ -z "$OPR_FLAG" ]]; then
  OPR_FLAG="$(pick_flag "$MAIN_PY" "--optimizer-ratio" "--optimizer_ratio")"
fi

MINW_FLAG="$(pick_flag "$MAIN_PY" "--min_width" "--min-width")"
MAXW_FLAG="$(pick_flag "$MAIN_PY" "--max_width" "--max-width")"

# Hard requirements: must be able to pass width & opratio via CLI (since we don't touch JSON)
[[ -n "$OPR_FLAG" ]] || { echo "[ERROR] $MAIN_PY does not support optimizer_ratio flag (--opratio/--optimizer_ratio/--optimizer-ratio)." >&2; exit 2; }
[[ -n "$MINW_FLAG" && -n "$MAXW_FLAG" ]] || { echo "[ERROR] $MAIN_PY does not support min/max width flags." >&2; exit 2; }

echo "[INFO] Using flags: opr='$OPR_FLAG' minw='$MINW_FLAG' maxw='$MAXW_FLAG' (MIN_WIDTH=$MIN_WIDTH MAX_WIDTH=$MAX_WIDTH)"
[[ -n "$DEBUG_FLAG" ]] && echo "[INFO] Debug enabled: passing --debug to $MAIN_PY"

run_case() {
  if [[ "$#" -lt 7 ]]; then
    echo "skip malformed CASE (need 7 fields): [$*]" >&2
    return 0
  fi
  local bench="$1" wtype="$2" round_size="$3" rng="$4" alpha="$5" beta="$6" opr="$7"
  local invoke; invoke="$(gen_invoke "$rng")"

  echo "=== ${ALGO} | ${bench} ${wtype} | round_size=${round_size} | invoke=${rng} | a=${alpha} b=${beta} op=${opr} | width=[${MIN_WIDTH},${MAX_WIDTH}] ==="

  PYTHONUNBUFFERED=1 python -u "$MAIN_PY" \
    "${ALGO}" "${bench}" "${wtype}" "${round_size}" "${invoke}" optimizer \
    "$ALPHA_FLAG" "${alpha}" \
    "$BETA_FLAG"  "${beta}"  \
    "$OPR_FLAG"   "${opr}"   \
    "$MINW_FLAG"  "${MIN_WIDTH}" \
    "$MAXW_FLAG"  "${MAX_WIDTH}" \
    ${DEBUG_FLAG}
}

# bench  type  round_size  range  alpha  beta  opratio
CASES=(
  "tpch  shifting  5   0-78    0.55 1.1  0.5"
  "tpch  noisy     5   0-94    0.70 1.1  0.5"
  "tpch  random    21  0-23    0.50 0.8  0.5"

  "tpchs shifting  5   0-78    0.85 1.1  0.5"
  "tpchs noisy     5   0-94    0.60 0.9  0.5"
  "tpchs random    21  0-23    0.35 1.1  0.5"

  "job   shifting  8   0-78    0.90 1.1  0.25"
  "job   noisy     8   0-94    0.70 1.5  0.25"
  "job   random    33  0-23    0.80 1.5  0.25"
)



for line in "${CASES[@]}"; do
  [[ -z "${line//[[:space:]]/}" ]] && continue
  [[ "${line:0:1}" == "#" || "${line:0:1}" == "%" ]] && continue
  IFS=' ' read -r bench wtype round rng alpha beta opr <<< "$line"
  run_case "$bench" "$wtype" "$round" "$rng" "$alpha" "$beta" "$opr"
done

#env MIN_WIDTH=1 MAX_WIDTH=3 ./scripts/liteselectmc_topk.sh