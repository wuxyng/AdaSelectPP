#!/usr/bin/env bash
# scripts/run_baselines_snapshot.sh
set -Eeuo pipefail
IFS=$'\n\t'

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 127; }; }
need jq
need python

DATE_TAG="${1:-$(date +%Y%m%d_%H%M%S)}"
SEED="${2:-${SEED:-0}}"
EVAL_METHOD="${EVAL_METHOD:-optimizer}"     # optimizer | tcnn (if you have it)
OSC_W="${OSC_W:-20}"                        # window size for oscillation_rate (Phase 0.2)
KEEP_LOG="${KEEP_LOG:-0}"                   # 0: move out of log/ ; 1: copy and keep
OUT_ROOT="results/baseline_snapshot/${DATE_TAG}"
mkdir -p "${OUT_ROOT}"

export DATE_TAG SEED EVAL_METHOD OSC_W OUT_ROOT

# ---------------------------
# Targets: label|algo|main_py_rel|cfg_rel
#   algo: main.py registry name
# ---------------------------
TARGETS=(
  "LS-Fix|LiteSelectMC_topk|litesel/main.py|litesel/config/liteselectmc_topk.json"
  "AS-TS|AdaSelect|adasel/main.py|adasel/config/adaselect.json"
)

# ---------------------------
# Modes:
#   w1: 单列（min_width=1,max_width=1）
#   w2: 多列（min_width=1,max_width=2）
# override: MODES="w2" or MODES="w1"
# ---------------------------
MODES_STR="${MODES:-w1 w2}"
MODES_STR="$(printf '%s' "$MODES_STR" | tr -d $'\r' | tr ',' ' ')"

old_ifs="$IFS"
IFS=' '
read -r -a MODES <<< "$MODES_STR"
IFS="$old_ifs"

# 兜底
[[ "${#MODES[@]}" -gt 0 ]] || MODES=(w1 w2)


# ---- experiment matrix ----
# 7 fields: bench  type  round  range  alpha  beta  opratio
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

strip_cr() { printf '%s' "$1" | tr -d $'\r'; }

gen_invoke() {
  local rng; rng="$(strip_cr "$1")"
  if [[ "${rng:0:1}" == "[" && "${rng: -1}" == "]" ]]; then
    echo "$rng"; return 0
  fi
  local a="${rng%-*}"; a="${a//[[:space:]]/}"
  local b="${rng#*-}"; b="${b//[[:space:]]/}"
  local out="[" i
  for (( i=a; i<=b; i++ )); do out+="${i},"; done
  echo "${out%,}]"
}

# Update config in-place; only touches keys that exist.
update_cfg() {
  local cfg="$1" alpha="$2" beta="$3" opr="$4" seed="$5" minw="$6" maxw="$7"
  alpha="$(strip_cr "$alpha")"; beta="$(strip_cr "$beta")"; opr="$(strip_cr "$opr")"
  seed="$(strip_cr "$seed")";  minw="$(strip_cr "$minw")"; maxw="$(strip_cr "$maxw")"

  local tmp; tmp="$(mktemp)"
  jq -c \
    --arg a "$alpha" --arg b "$beta" --arg o "$opr" \
    --arg sd "$seed" --arg mn "$minw" --arg mx "$maxw" \
    '
    .alpha = ($a|tonumber)
    | .beta = ($b|tonumber)
    | (if has("optimizer_ratio") then .optimizer_ratio = ($o|tonumber) else . end)
    | (if has("ratio")           then .ratio           = ($o|tonumber) else . end)
    | (if has("seed")            then .seed            = ($sd|tonumber) else . end)
    | (if has("min_width")       then .min_width       = ($mn|tonumber) else . end)
    | (if has("max_width")       then .max_width       = ($mx|tonumber) else . end)
    ' \
    "$cfg" > "$tmp"
  mv -f "$tmp" "$cfg"
}

read_cfg_field() { jq -r "$2 // empty" "$1"; }

dump_manifest() {
  local path="${OUT_ROOT}/manifest.txt"
  {
    echo "DATE_TAG=${DATE_TAG}"
    echo "REPO_ROOT=${REPO_ROOT}"
    echo "SEED=${SEED}"
    echo "EVAL_METHOD=${EVAL_METHOD}"
    echo "OSC_W=${OSC_W}"
    echo "PYTHON=$(python -V 2>&1)"
    if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      echo "GIT_COMMIT=$(git rev-parse HEAD 2>/dev/null || true)"
      echo "GIT_STATUS="
      git status --porcelain || true
    fi
  } > "$path"
}
dump_manifest

# ---- config backups (restore on exit) ----
declare -A CFG_BACKUP
cleanup() {
  for k in "${!CFG_BACKUP[@]}"; do
    local cfg="${k}"
    local bkp="${CFG_BACKUP[$k]}"
    [[ -f "$bkp" ]] && cp -f "$bkp" "$cfg" || true
    rm -f "$bkp" || true
  done
}
trap cleanup EXIT INT TERM

for target in "${TARGETS[@]}"; do
  IFS='|' read -r label algo main_py cfg_rel <<< "$target"
  IFS=$'\n\t'

  [[ -f "${REPO_ROOT}/${main_py}" ]] || { echo "Missing main.py: ${main_py}" >&2; exit 2; }
  [[ -f "${REPO_ROOT}/${cfg_rel}"  ]] || { echo "Missing config: ${cfg_rel}" >&2; exit 2; }

  # backup config once
  if [[ -z "${CFG_BACKUP[${REPO_ROOT}/${cfg_rel}]:-}" ]]; then
    bkp="$(mktemp)"
    cp -f "${REPO_ROOT}/${cfg_rel}" "$bkp"
    CFG_BACKUP["${REPO_ROOT}/${cfg_rel}"]="$bkp"
  fi

  for mode in "${MODES[@]}"; do
    case "$mode" in
      w1) minw=1; maxw=1 ;;
      w2) minw=1; maxw=2 ;;
      *) echo "Unknown mode: $mode (use w1/w2)" >&2; exit 2 ;;
    esac

    for line in "${CASES[@]}"; do
      [[ -z "${line//[[:space:]]/}" ]] && continue
      [[ "${line:0:1}" == "#" || "${line:0:1}" == "%" ]] && continue

      # parse 7 fields
      IFS=' ' read -r bench wtype round rng alpha beta opr <<< "$line"
      IFS=$'\n\t'

      invoke="$(gen_invoke "$rng")"

      run_dir="${OUT_ROOT}/${label}/${mode}/${bench}/${wtype}/a${alpha}_b${beta}_op${opr}_seed${SEED}"
      mkdir -p "$run_dir"

      # restore pristine cfg then update for this run
      cp -f "${CFG_BACKUP[${REPO_ROOT}/${cfg_rel}]}" "${REPO_ROOT}/${cfg_rel}"
      update_cfg "${REPO_ROOT}/${cfg_rel}" "$alpha" "$beta" "$opr" "$SEED" "$minw" "$maxw"

      alpha2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '.alpha')"
      beta2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '.beta')"
      opr2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '(.optimizer_ratio // .ratio)')"
      seed2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '.seed')"
      minw2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '.min_width')"
      maxw2="$(read_cfg_field "${REPO_ROOT}/${cfg_rel}" '.max_width')"

      mkdir -p "${REPO_ROOT}/log"
      prefix="${REPO_ROOT}/log/${algo}_${bench}_${wtype}_a${alpha2}_b${beta2}"

      # prevent mixing with stale leftovers
      rm -f "${prefix}"*.log "${prefix}"*.csv 2>/dev/null || true

      echo "=== RUN | ${label} | ${algo} | mode=${mode} (minw=${minw2:-NA} maxw=${maxw2:-NA}) | ${bench} ${wtype} | round=${round} | invoke=${rng} | a=${alpha2} b=${beta2} op=${opr2:-NA} seed=${seed2:-NA} ==="

      set +e
      (
        cd "${REPO_ROOT}"
        PYTHONUNBUFFERED=1 python -u "${main_py}" \
          "${algo}" "${bench}" "${wtype}" "${round}" "${invoke}" "${EVAL_METHOD}"
      ) 2>&1 | tee "${run_dir}/console.log"
      rc="${PIPESTATUS[0]}"
      set -e

      echo "${rc}" > "${run_dir}/exit_code.txt"
      if [[ "$rc" -ne 0 ]]; then
        echo "[WARN] run failed (exit=${rc}) -> ${run_dir}" >&2
      fi

      # archive: move all prefix-matched artifacts out of log/
      shopt -s nullglob
      moved_any=0
      for f in "${prefix}"*; do
        moved_any=1
        if [[ "${KEEP_LOG}" == "1" ]]; then
          cp -f "$f" "${run_dir}/"
        else
          mv -f "$f" "${run_dir}/"
        fi
      done
      shopt -u nullglob

      # normalize names if present
      # (main per-round csv)
      per_csv=""
      if ls "${run_dir}/${algo}_${bench}_${wtype}_a${alpha2}_b${beta2}.csv" >/dev/null 2>&1; then
        per_csv="${run_dir}/${algo}_${bench}_${wtype}_a${alpha2}_b${beta2}.csv"
      else
        # fallback: first csv that is not trace
        cand=()
        while IFS= read -r -d '' x; do cand+=("$x"); done < <(find "${run_dir}" -maxdepth 1 -type f -name "*.csv" ! -name "*trace.csv" -print0)
        if [[ "${#cand[@]}" -gt 0 ]]; then per_csv="${cand[0]}"; fi
      fi
      if [[ -n "${per_csv}" && -f "${per_csv}" ]]; then
        cp -f "${per_csv}" "${run_dir}/per_round.csv"
      fi

      # (main log)
      if [[ -f "${run_dir}/${algo}_${bench}_${wtype}_a${alpha2}_b${beta2}.log" ]]; then
        cp -f "${run_dir}/${algo}_${bench}_${wtype}_a${alpha2}_b${beta2}.log" "${run_dir}/run.log"
      fi

      # config used
      cp -f "${REPO_ROOT}/${cfg_rel}" "${run_dir}/config_used.json"

      # meta
      {
        echo "label=${label}"
        echo "algo=${algo}"
        echo "mode=${mode}"
        echo "bench=${bench}"
        echo "wtype=${wtype}"
        echo "alpha=${alpha2}"
        echo "beta=${beta2}"
        echo "opratio=${opr2:-NA}"
        echo "seed=${seed2:-NA}"
        echo "min_width=${minw2:-NA}"
        echo "max_width=${maxw2:-NA}"
        echo "eval_method=${EVAL_METHOD}"
        echo "osc_w=${OSC_W}"
        echo "invoke=${invoke}"
        echo "exit_code=${rc}"
        echo "cmd=python -u ${main_py} ${algo} ${bench} ${wtype} ${round} '${invoke}' ${EVAL_METHOD}"
        wl="${REPO_ROOT}/database/workload/${bench}_${wtype}.txt"
        if [[ -f "$wl" ]]; then
          echo "workload_file=${wl}"
          echo "workload_lines=$(wc -l < "$wl" | tr -d ' ')"
          if command -v sha256sum >/dev/null 2>&1; then
            echo "workload_sha256=$(sha256sum "$wl" | awk '{print $1}')"
          fi
        fi
      } > "${run_dir}/run_meta.txt"
    done
  done
done

# ---------------------------
# Regression summary: index.csv + metrics_plus.csv
# ---------------------------
python - <<'PY'
import csv, os, math, ast
from pathlib import Path
from statistics import mean

out_root = Path(os.environ["OUT_ROOT"])
OSC_W = int(os.environ.get("OSC_W", "20"))

INDEX_FIELDS = [
  "label","algo","mode","bench","wtype","alpha","beta","opratio","seed","run_dir","exit_code",
  "final_total","mean_total","p95_total","mean_exec","mean_rec","mean_trans",
  "switch_rounds_flag","switch_total","stability_score",
  "switch_total_W","osc_repeat_total_W","oscillation_rate_W",
  "osc_indices_W","osc_index_fraction_W",
  "osc_repeat_total_full","oscillation_rate_full",
  "whatif_total"
]

PLUS_FIELDS = [
  "label","algo","mode","bench","wtype","alpha","beta","opratio","seed","osc_w",
  "index_key","toggle_full","toggle_W","repeat_full","repeat_W","run_dir"
]

def quantile_nearest_rank(vals, q: float):
  vals = [v for v in vals if v is not None and not math.isnan(v)]
  if not vals: return float("nan")
  vals = sorted(vals)
  k = int(math.ceil(q * len(vals))) - 1
  k = max(0, min(k, len(vals)-1))
  return float(vals[k])

def parse_set(cell):
  if cell is None: return set()
  s = str(cell).strip()
  if not s or s.lower() == "nan": return set()
  try:
    obj = ast.literal_eval(s)
  except Exception:
    return set()
  try:
    return {repr(x) for x in obj}
  except Exception:
    return set()

def read_meta(meta_path: Path):
  d = {}
  for line in meta_path.read_text(encoding="utf-8", errors="ignore").splitlines():
    if "=" in line:
      k, v = line.split("=", 1)
      d[k.strip()] = v.strip()
  return d

def fnum(x):
  try: return float(x)
  except Exception: return float("nan")

index_rows = []
plus_rows = []

for meta_path in out_root.rglob("run_meta.txt"):
  run_dir = meta_path.parent
  meta = read_meta(meta_path)
  exit_code = meta.get("exit_code", meta.get("exit", ""))

  per_csv = run_dir / "per_round.csv"
  if not per_csv.exists():
    # still write a stub row so gate can see failures
    row = {k:"" for k in INDEX_FIELDS}
    row.update({
      "label": meta.get("label",""),
      "algo": meta.get("algo",""),
      "mode": meta.get("mode",""),
      "bench": meta.get("bench",""),
      "wtype": meta.get("wtype",""),
      "alpha": meta.get("alpha",""),
      "beta": meta.get("beta",""),
      "opratio": meta.get("opratio",""),
      "seed": meta.get("seed",""),
      "run_dir": str(run_dir),
      "exit_code": exit_code,
    })
    index_rows.append(row)
    continue

  with per_csv.open("r", newline="", encoding="utf-8", errors="ignore") as f:
    rd = csv.DictReader(f)
    rows = list(rd)

  # column aliases (LiteSelect uses what_if_calls)
  def col(r, *names):
    for n in names:
      if n in r and r[n] != "": return r[n]
    return ""

  totals = [fnum(col(r, "total")) for r in rows]
  execs  = [fnum(col(r, "exec")) for r in rows]
  recs   = [fnum(col(r, "rec")) for r in rows]
  trans  = [fnum(col(r, "trans")) for r in rows]
  switched_flag = [fnum(col(r, "switched")) for r in rows]
  whatif_calls  = [fnum(col(r, "what_if_calls", "whatif_calls")) for r in rows]

  # configs per round
  S = [parse_set(col(r, "new")) for r in rows]
  deltas = []
  for t in range(1, len(S)):
    deltas.append(S[t-1] ^ S[t])

  switch_total = float(sum(len(d) for d in deltas))
  sum_set_size = float(sum(len(s) for s in S))
  stability_score = 1.0 - (switch_total / (sum_set_size + 1e-9))

  W = min(OSC_W, len(deltas))
  deltas_W = deltas[-W:] if W > 0 else []
  switch_total_W = float(sum(len(d) for d in deltas_W))

  # toggle counts per index
  toggle_full = {}
  toggle_W = {}
  for d in deltas:
    for idx in d:
      toggle_full[idx] = toggle_full.get(idx, 0) + 1
  for d in deltas_W:
    for idx in d:
      toggle_W[idx] = toggle_W.get(idx, 0) + 1

  # STRICT Phase-0.2 style (index-aggregated repeats):
  # repeats_i = max(0, toggles_i - 1); sum over indices; normalize by total toggles in window
  osc_repeat_total_W = float(sum(max(0, c - 1) for c in toggle_W.values()))
  oscillation_rate_W = osc_repeat_total_W / max(1.0, switch_total_W)

  osc_repeat_total_full = float(sum(max(0, c - 1) for c in toggle_full.values()))
  oscillation_rate_full = osc_repeat_total_full / max(1.0, switch_total)

  # system-level helper: fraction of indices that toggled >=2 in window
  osc_indices_W = sum(1 for c in toggle_W.values() if c >= 2)
  osc_index_fraction_W = osc_indices_W / max(1.0, len(toggle_W))

  # plus rows
  for idx, cfull in sorted(toggle_full.items(), key=lambda x: (-x[1], x[0])):
    cW = toggle_W.get(idx, 0)
    plus_rows.append({
      "label": meta.get("label",""),
      "algo": meta.get("algo",""),
      "mode": meta.get("mode",""),
      "bench": meta.get("bench",""),
      "wtype": meta.get("wtype",""),
      "alpha": meta.get("alpha",""),
      "beta": meta.get("beta",""),
      "opratio": meta.get("opratio",""),
      "seed": meta.get("seed",""),
      "osc_w": meta.get("osc_w", str(OSC_W)),
      "index_key": idx,
      "toggle_full": cfull,
      "toggle_W": cW,
      "repeat_full": max(0, cfull-1),
      "repeat_W": max(0, cW-1),
      "run_dir": str(run_dir),
    })

  good_totals = [v for v in totals if not math.isnan(v)]
  good_exec   = [v for v in execs  if not math.isnan(v)]
  good_rec    = [v for v in recs   if not math.isnan(v)]
  good_trans  = [v for v in trans  if not math.isnan(v)]
  good_whatif = [v for v in whatif_calls if not math.isnan(v)]

  row = {k:"" for k in INDEX_FIELDS}
  row.update({
    "label": meta.get("label",""),
    "algo": meta.get("algo",""),
    "mode": meta.get("mode",""),
    "bench": meta.get("bench",""),
    "wtype": meta.get("wtype",""),
    "alpha": meta.get("alpha",""),
    "beta": meta.get("beta",""),
    "opratio": meta.get("opratio",""),
    "seed": meta.get("seed",""),
    "run_dir": str(run_dir),
    "exit_code": exit_code,
    "final_total": good_totals[-1] if good_totals else float("nan"),
    "mean_total": mean(good_totals) if good_totals else float("nan"),
    "p95_total": quantile_nearest_rank(good_totals, 0.95),
    "mean_exec": mean(good_exec) if good_exec else float("nan"),
    "mean_rec": mean(good_rec) if good_rec else float("nan"),
    "mean_trans": mean(good_trans) if good_trans else float("nan"),
    "switch_rounds_flag": float(sum(1 for v in switched_flag if v >= 0.5)),
    "switch_total": switch_total,
    "stability_score": stability_score,
    "switch_total_W": switch_total_W,
    "osc_repeat_total_W": osc_repeat_total_W,
    "oscillation_rate_W": oscillation_rate_W,
    "osc_indices_W": osc_indices_W,
    "osc_index_fraction_W": osc_index_fraction_W,
    "osc_repeat_total_full": osc_repeat_total_full,
    "oscillation_rate_full": oscillation_rate_full,
    "whatif_total": float(sum(good_whatif)) if good_whatif else float("nan"),
  })
  index_rows.append(row)

def write_csv(path: Path, fields, rows):
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("w", newline="", encoding="utf-8") as f:
    wr = csv.DictWriter(f, fieldnames=fields)
    wr.writeheader()
    for r in rows:
      wr.writerow({k: r.get(k, "") for k in fields})

out_index = out_root / "index.csv"
out_plus  = out_root / "metrics_plus.csv"
write_csv(out_index, INDEX_FIELDS, index_rows)
write_csv(out_plus,  PLUS_FIELDS,  plus_rows)

print("[OK] wrote:", out_index)
print("[OK] wrote:", out_plus)
PY

echo "[OK] Baseline snapshots saved to: ${OUT_ROOT}"
echo "[OK] Index:   ${OUT_ROOT}/index.csv"
echo "[OK] Metrics: ${OUT_ROOT}/metrics_plus.csv"

# Usage examples:
#   bash scripts/run_baselines_snapshot.sh
#   bash scripts/run_baselines_snapshot.sh 20251214_baseline0p1 0
#   MODES="w2" bash scripts/run_baselines_snapshot.sh
#   KEEP_LOG=1 bash scripts/run_baselines_snapshot.sh
