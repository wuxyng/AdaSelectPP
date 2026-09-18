#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 127; }; }
need python

MAIN_PY="adasel/main.py"
ALGO="${ALGO:-AdaSelect}"

DEBUG_FLAG=""
if [[ "${1:-}" == "--debug" ]]; then
  DEBUG_FLAG="--debug"
  shift
elif [[ "${DEBUG:-0}" == "1" ]]; then
  DEBUG_FLAG="--debug"
fi

TRACE_FLAG=""
if [[ "${TRACE:-0}" == "1" ]]; then
  TRACE_FLAG="--trace"
fi

MIN_WIDTH="${MIN_WIDTH:-1}"
MAX_WIDTH="${MAX_WIDTH:-2}"

LAM_POLICIES_STR="${LAM_POLICIES:-adaptive fixed}"
WDCG_VALUES_STR="${WDCG_VALUES:-0 1}"
CASES_FILTER="${CASES_FILTER:-}"

IFS=' ' read -r -a LAM_POLICIES_ARR <<< "$LAM_POLICIES_STR"
IFS=' ' read -r -a WDCG_VALUES_ARR <<< "$WDCG_VALUES_STR"

gen_invoke() {
  local rng="$1"
  [[ "$rng" == \[*\] ]] && { echo "$rng"; return; }
  local a="${rng%-*}" b="${rng#*-}" out="[" i
  for ((i=a;i<=b;i++)); do out+="${i},"; done
  echo "${out%,}]"
}

case_matches_filter() {
  local line="$1"
  local filt="${CASES_FILTER:-}"
  [[ -z "$filt" ]] && return 0

  local norm_line
  norm_line="$(printf '%s' "$line" | tr -s '[:space:]' ' ')"

  local oldifs="$IFS"
  local -a terms=()
  IFS=' '
  read -r -a terms <<< "$filt"
  IFS="$oldifs"

  local term
  for term in "${terms[@]}"; do
    [[ -z "$term" ]] && continue
    if [[ "$norm_line" != *"$term"* ]]; then
      return 1
    fi
  done
  return 0
}

ALPHA_FLAG="--alpha"
BETA_FLAG="--beta"
LAM_POLICY_FLAG="--lambda_policy"
WDCG_FLAG="--wdcg_enabled"
FIXLAM_FLAG="--fixed_lambda"
OPR_FLAG="--opratio"
MINW_FLAG="--min_width"
MAXW_FLAG="--max_width"

echo "[INFO] sweep start: algo=${ALGO} lambda_policies='${LAM_POLICIES_STR}' wdcg_values='${WDCG_VALUES_STR}' width=[${MIN_WIDTH},${MAX_WIDTH}] filter='${CASES_FILTER}'"

run_case() {
  if [[ "$#" -lt 7 ]]; then
    echo "skip malformed CASE (need 7 fields): [$*]" >&2
    return 0
  fi

  local bench="$1" wtype="$2" round_size="$3" rng="$4" alpha="$5" beta="$6" opr="$7"
  local invoke
  invoke="$(gen_invoke "$rng")"

  local lam_policy
  local wdcg
  for lam_policy in "${LAM_POLICIES_ARR[@]}"; do
    for wdcg in "${WDCG_VALUES_ARR[@]}"; do
      echo "=== ${ALGO} | ${bench} ${wtype} | round=${round_size} | invoke=${rng} | lambda=${alpha} beta=${beta} op=${opr} | policy=${lam_policy} wdcg=${wdcg} | width=[${MIN_WIDTH},${MAX_WIDTH}] ==="

      if [[ -n "$FIXLAM_FLAG" ]]; then
        PYTHONUNBUFFERED=1 python -u "$MAIN_PY" \
          "${ALGO}" "${bench}" "${wtype}" "${round_size}" "${invoke}" optimizer \
          "$ALPHA_FLAG"      "${alpha}" \
          "$FIXLAM_FLAG"     "${alpha}" \
          "$BETA_FLAG"       "${beta}" \
          "$OPR_FLAG"        "${opr}" \
          "$MINW_FLAG"       "${MIN_WIDTH}" \
          "$MAXW_FLAG"       "${MAX_WIDTH}" \
          "$LAM_POLICY_FLAG" "${lam_policy}" \
          "$WDCG_FLAG"       "${wdcg}" \
          ${TRACE_FLAG} \
          ${DEBUG_FLAG}
      else
        PYTHONUNBUFFERED=1 python -u "$MAIN_PY" \
          "${ALGO}" "${bench}" "${wtype}" "${round_size}" "${invoke}" optimizer \
          "$ALPHA_FLAG"      "${alpha}" \
          "$BETA_FLAG"       "${beta}" \
          "$OPR_FLAG"        "${opr}" \
          "$MINW_FLAG"       "${MIN_WIDTH}" \
          "$MAXW_FLAG"       "${MAX_WIDTH}" \
          "$LAM_POLICY_FLAG" "${lam_policy}" \
          "$WDCG_FLAG"       "${wdcg}" \
          ${TRACE_FLAG} \
          ${DEBUG_FLAG}
      fi
    done
  done
}

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

matched=0
for line in "${CASES[@]}"; do
  [[ -z "${line//[[:space:]]/}" ]] && continue
  [[ "${line:0:1}" == "#" || "${line:0:1}" == "%" ]] && continue
  if ! case_matches_filter "$line"; then
    continue
  fi
  IFS=' ' read -r bench wtype round_size rng alpha beta opr <<< "$line"
  run_case "$bench" "$wtype" "$round_size" "$rng" "$alpha" "$beta" "$opr"
  matched=1
done

if [[ "$matched" -eq 0 ]]; then
  echo "[WARN] No CASE matched CASES_FILTER='${CASES_FILTER}'" >&2
  exit 0
fi