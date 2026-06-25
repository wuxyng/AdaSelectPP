#!/usr/bin/env python3
"""PR20e broader offline replay for the movie_info prefix swap.

This diagnostic extends PR20d by broadening round selection and by reporting
run-order, variance, unstable exclusions, query concentration, and descriptive
ordering agreement. It remains offline-only and does not change any online
AdaSelectPP policy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.pr19_candidate_pool_common import IndexKey, format_candidate_key, normalize_candidate_key
from tools.pr20c_swap_width2_oracle import relative_improvement
from tools.pr20d_real_exec_prefix_swap import (
    DEFAULT_COMPOSITE_INDEX,
    DEFAULT_PREFIX_INDEX,
    IndexDDL,
    _execute_analyze_plan,
    _fmt,
    build_prefix_swap_config,
    config_to_string,
    drop_config_indexes,
    ensure_config_capacity,
    ensure_experimental_allowed,
    load_workloads,
    materialize_config,
    plan_uses_index,
    quote_ident,
    read_executed_configs,
    read_pr20c_round_rows,
    read_pr20c_target_rows,
)


OUTPUT_ROOT = Path("runs_pr20e_broader_prefix_swap_replay")
WHATIF_WIN_THRESHOLD = 0.005
OUTCOME_THRESHOLD = 0.01
DEFAULT_MAX_CV = 0.20
DESCRIPTIVE_ONLY_LABEL = "DESCRIPTIVE ONLY: ordering agreement, not calibration."

ROUND_COLUMNS = [
    "round_id",
    "sample_category",
    "unstable_excluded",
    "unstable_reason",
    "pr20c_whatif_rel_improvement",
    "baseline_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "baseline_exec_ms_mean",
    "swap_exec_ms_mean",
    "baseline_exec_ms_stdev",
    "swap_exec_ms_stdev",
    "baseline_cv",
    "swap_cv",
    "run_order_id",
    "run_order",
    "real_exec_delta_ms",
    "real_exec_rel_improvement",
    "outcome",
    "plan_uses_prefix_count",
    "plan_uses_composite_count",
    "query_level_concentration",
    "num_queries",
    "notes",
]

QUERY_COLUMNS = [
    "round_id",
    "sample_category",
    "query_id",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_rel_improvement",
    "plan_uses_prefix_index",
    "plan_uses_composite_index",
    "notes",
]

SUMMARY_COLUMNS = [
    "row_type",
    "sample_category",
    "round_count",
    "mean_real_exec_rel_improvement",
    "median_real_exec_rel_improvement",
    "min_real_exec_rel_improvement",
    "max_real_exec_rel_improvement",
    "improved_count",
    "worse_count",
    "flat_count",
    "excluded_round_count",
    "excluded_round_ids",
    "excluded_reason_counts",
    "baseline_cv_summary",
    "swap_cv_summary",
    "spearman_rank_correlation",
    "sign_agreement_rate",
    "ordering_diagnostic_label",
    "notes",
]

EXCLUDED_COLUMNS = [
    "round_id",
    "original_sample_category",
    "unstable_reason",
    "baseline_cv",
    "swap_cv",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "pr20c_whatif_rel_improvement",
    "notes",
]


@dataclass(frozen=True)
class SelectedRound:
    round_id: int
    sample_category: str
    pr20c_whatif_rel_improvement: float
    notes: str = ""


@dataclass
class WorkloadRun:
    total_ms: float
    per_query_ms: List[float]
    plan_uses_index: List[bool]


def _row_float(row: Mapping[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(str(row.get(key, "")).strip())
    except Exception:
        return default


def _row_true(row: Mapping[str, str], key: str) -> bool:
    return str(row.get(key, "")).strip().lower() in {"1", "1.0", "true", "yes"}


def _split_win_categories(wins: Sequence[Tuple[int, float]]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    n = len(wins)
    if n == 0:
        return out
    top_cut = int(math.ceil(n / 3.0))
    mid_cut = int(math.ceil(2 * n / 3.0))
    for pos, (rid, _rel) in enumerate(wins):
        if pos < top_cut:
            out[rid] = "top_win"
        elif pos < mid_cut:
            out[rid] = "mid_win"
        else:
            out[rid] = "low_win"
    return out


def _middle_slice(items: Sequence[Tuple[int, float]], count: int) -> List[Tuple[int, float]]:
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    start = max(0, (len(items) - count) // 2)
    return list(items[start:start + count])


def select_broader_rounds(
    *,
    rounds_csv: Path,
    candidates_csv: Path,
    composite_index: IndexKey,
    selection_mode: str = "all",
    max_rounds: int = 0,
    whatif_threshold: float = WHATIF_WIN_THRESHOLD,
    top_count: int = 5,
    mid_count: int = 5,
    low_count: int = 5,
    control_count: int = 5,
) -> List[SelectedRound]:
    """Select target-pattern rounds and label top/mid/low/control strata."""
    composite_name = format_candidate_key(composite_index)
    round_rows = read_pr20c_round_rows(rounds_csv)
    target_rows = read_pr20c_target_rows(candidates_csv, composite_index)

    target_best_rounds: List[Tuple[int, float]] = []
    for rid, row in round_rows.items():
        if str(row.get("best_swap_index", "")).strip() != composite_name:
            continue
        target = target_rows.get(rid, {})
        rel = _row_float(target, "swap_relative_improvement", _row_float(row, "best_swap_relative_improvement"))
        target_best_rounds.append((rid, rel))

    wins = sorted(
        [(rid, rel) for rid, rel in target_best_rounds if rel >= whatif_threshold and _row_true(target_rows.get(rid, {}), "oracle_pass_swap")],
        key=lambda item: (-item[1], item[0]),
    )
    controls = sorted(
        [(rid, rel) for rid, rel in target_best_rounds if not (rel >= whatif_threshold and _row_true(target_rows.get(rid, {}), "oracle_pass_swap"))],
        key=lambda item: (-item[1], item[0]),
    )

    use_stratified = str(selection_mode).strip().lower() == "stratified"
    if max_rounds and len(target_best_rounds) > int(max_rounds):
        use_stratified = True

    selected: List[SelectedRound] = []
    if not use_stratified:
        category_by_round = _split_win_categories(wins)
        for rid, rel in wins:
            selected.append(SelectedRound(rid, category_by_round[rid], rel, "all_target_best_rounds"))
        for rid, rel in controls:
            selected.append(SelectedRound(rid, "control", rel, "all_target_best_rounds"))
        return sorted(selected, key=lambda item: item.round_id)

    chosen: Dict[int, SelectedRound] = {}
    for rid, rel in wins[:max(0, int(top_count))]:
        chosen[rid] = SelectedRound(rid, "top_win", rel, "stratified_top")
    for rid, rel in _middle_slice(wins, max(0, int(mid_count))):
        chosen.setdefault(rid, SelectedRound(rid, "mid_win", rel, "stratified_mid"))
    low_items = wins[-max(0, int(low_count)):] if low_count else []
    for rid, rel in low_items:
        chosen.setdefault(rid, SelectedRound(rid, "low_win", rel, "stratified_low"))
    for rid, rel in controls[:max(0, int(control_count))]:
        chosen.setdefault(rid, SelectedRound(rid, "control", rel, "stratified_control"))
    if max_rounds and len(chosen) > int(max_rounds):
        ordered = sorted(chosen.values(), key=lambda item: (item.sample_category, -item.pr20c_whatif_rel_improvement, item.round_id))
        chosen = {item.round_id: item for item in ordered[:int(max_rounds)]}
    return sorted(chosen.values(), key=lambda item: item.round_id)


def pr20e_index_name(*, run_label: str, round_id: int, config_label: str, index: IndexKey) -> str:
    idx_text = format_candidate_key(index)
    digest = hashlib.md5(f"pr20e|{run_label}|{round_id}|{config_label}|{idx_text}".encode("utf-8")).hexdigest()[:10]
    table, cols = normalize_candidate_key(index)
    body = re.sub(r"[^a-zA-Z0-9_]+", "_", f"{run_label}_{round_id}_{config_label}_{table}_{'_'.join(cols)}".lower()).strip("_")
    prefix = "pr20e_"
    max_body = 63 - len(prefix) - len(digest) - 1
    return f"{prefix}{body[:max_body]}_{digest}"


def generate_pr20e_index_ddls(
    config: Iterable[IndexKey],
    *,
    run_label: str,
    round_id: int,
    config_label: str,
    max_num: int,
) -> List[IndexDDL]:
    normalized = {normalize_candidate_key(idx) for idx in config}
    ensure_config_capacity(normalized, max_num=max_num)
    ddls: List[IndexDDL] = []
    for idx in sorted(normalized, key=format_candidate_key):
        table, cols = idx
        name = pr20e_index_name(
            run_label=run_label,
            round_id=round_id,
            config_label=config_label,
            index=idx,
        )
        create_sql = f"CREATE INDEX {quote_ident(name)} ON {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)})"
        drop_sql = f"DROP INDEX IF EXISTS {quote_ident(name)}"
        ddls.append(IndexDDL(index=idx, name=name, create_sql=create_sql, drop_sql=drop_sql))
    return ddls


def make_run_order(repeats: int, run_order_id: str = "alternating_pairs") -> List[str]:
    if int(repeats) < 3:
        raise ValueError("PR20e requires repeats >= 3")
    if run_order_id != "alternating_pairs":
        raise ValueError(f"unsupported run_order_id: {run_order_id}")
    order: List[str] = []
    for i in range(int(repeats)):
        if i % 2 == 0:
            order.extend(["baseline", "swap"])
        else:
            order.extend(["swap", "baseline"])
    return order


def _execute_workload_once(db, workload: Sequence[str], watched_index_name: str) -> WorkloadRun:
    total = 0.0
    per_query: List[float] = []
    plan_uses: List[bool] = []
    for query in workload:
        runtime_ms, plan = _execute_analyze_plan(db, query)
        total += float(runtime_ms)
        per_query.append(float(runtime_ms))
        plan_uses.append(plan_uses_index(plan, watched_index_name))
    return WorkloadRun(total, per_query, plan_uses)


def median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def mean(values: Sequence[float]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    return float(statistics.stdev(values)) if len(values) >= 2 else 0.0


def coeff_var(values: Sequence[float], *, epsilon: float = 1e-9) -> float:
    med = median(values)
    return stdev(values) / max(abs(med), float(epsilon))


def unstable_reason_for(base_cv: float, swap_cv: float, max_cv: float) -> str:
    base_high = float(base_cv) > float(max_cv)
    swap_high = float(swap_cv) > float(max_cv)
    if base_high and swap_high:
        return "both_cv_high"
    if base_high:
        return "baseline_cv_high"
    if swap_high:
        return "swap_cv_high"
    return ""


def outcome_for_rel(rel: float, *, threshold: float = OUTCOME_THRESHOLD, unstable: bool = False) -> str:
    if unstable:
        return "excluded_unstable"
    if float(rel) >= float(threshold):
        return "improved"
    if float(rel) <= -float(threshold):
        return "worse"
    return "flat"


def query_concentration(deltas: Sequence[float], *, top_k: int = 4) -> float:
    positives = sorted([float(x) for x in deltas if float(x) > 0.0], reverse=True)
    denom = sum(positives)
    if denom <= 0.0:
        return 0.0
    return sum(positives[:int(top_k)]) / denom


def _json_list(values: Sequence[object]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _json_float_list(values: Sequence[float]) -> str:
    return json.dumps([round(float(v), 6) for v in values], separators=(",", ":"))


def _pick_index_name(ddls: Sequence[IndexDDL], index: IndexKey) -> str:
    target = normalize_candidate_key(index)
    return next((ddl.name for ddl in ddls if normalize_candidate_key(ddl.index) == target), "")


def evaluate_round(
    *,
    db,
    selected_round: SelectedRound,
    workload: Sequence[str],
    baseline_config: Set[IndexKey],
    prefix_index: IndexKey,
    composite_index: IndexKey,
    max_num: int,
    warmup: int,
    repeats: int,
    max_cv: float,
    outcome_threshold: float,
    run_label: str,
    run_order_id: str,
) -> Tuple[Mapping[str, object], List[Mapping[str, object]], Optional[Mapping[str, object]]]:
    if int(warmup) < 1:
        raise ValueError("PR20e requires warmup >= 1")
    order = make_run_order(repeats, run_order_id)
    swap_config, feasible, reason = build_prefix_swap_config(
        baseline_config,
        prefix_index,
        composite_index,
        max_num=max_num,
    )
    if not feasible or swap_config is None:
        row = {
            "round_id": selected_round.round_id,
            "sample_category": "excluded_unstable",
            "unstable_excluded": 1,
            "unstable_reason": reason,
            "pr20c_whatif_rel_improvement": selected_round.pr20c_whatif_rel_improvement,
            "baseline_config": config_to_string(baseline_config),
            "swap_config": "",
            "prefix_index": format_candidate_key(prefix_index),
            "composite_index": format_candidate_key(composite_index),
            "outcome": "excluded_unstable",
            "notes": f"original_category={selected_round.sample_category};{selected_round.notes}",
        }
        return row, [], row

    baseline_ddls = generate_pr20e_index_ddls(
        baseline_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="baseline",
        max_num=max_num,
    )
    swap_ddls = generate_pr20e_index_ddls(
        swap_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="swap",
        max_num=max_num,
    )
    prefix_name = _pick_index_name(baseline_ddls, prefix_index)
    composite_name = _pick_index_name(swap_ddls, composite_index)

    totals = {"baseline": [], "swap": []}
    per_query = {
        "baseline": [[] for _ in workload],
        "swap": [[] for _ in workload],
    }
    plan_uses = {
        "baseline": [False for _ in workload],
        "swap": [False for _ in workload],
    }
    warmed = {"baseline": False, "swap": False}
    ddls_by_label = {"baseline": baseline_ddls, "swap": swap_ddls}
    watched_by_label = {"baseline": prefix_name, "swap": composite_name}

    for label in order:
        ddls = ddls_by_label[label]
        try:
            materialize_config(db, ddls)
            if not warmed[label]:
                for _ in range(int(warmup)):
                    _execute_workload_once(db, workload, watched_by_label[label])
                warmed[label] = True
            result = _execute_workload_once(db, workload, watched_by_label[label])
        finally:
            drop_config_indexes(db, ddls)
        totals[label].append(result.total_ms)
        for qid, runtime in enumerate(result.per_query_ms):
            per_query[label][qid].append(runtime)
        for qid, used in enumerate(result.plan_uses_index):
            plan_uses[label][qid] = bool(plan_uses[label][qid] or used)

    baseline_median = median(totals["baseline"])
    swap_median = median(totals["swap"])
    base_cv = coeff_var(totals["baseline"])
    swap_cv = coeff_var(totals["swap"])
    unstable_reason = unstable_reason_for(base_cv, swap_cv, max_cv)
    unstable = bool(unstable_reason)
    real_delta = baseline_median - swap_median
    real_rel = relative_improvement(baseline_median, swap_median)
    outcome = outcome_for_rel(real_rel, threshold=outcome_threshold, unstable=unstable)

    query_rows: List[Mapping[str, object]] = []
    query_deltas: List[float] = []
    for qid in range(len(workload)):
        base_q = median(per_query["baseline"][qid])
        swap_q = median(per_query["swap"][qid])
        delta_q = base_q - swap_q
        query_deltas.append(delta_q)
        query_rows.append({
            "round_id": selected_round.round_id,
            "sample_category": "excluded_unstable" if unstable else selected_round.sample_category,
            "query_id": qid,
            "baseline_exec_ms_median": base_q,
            "swap_exec_ms_median": swap_q,
            "exec_delta_ms": delta_q,
            "exec_rel_improvement": relative_improvement(base_q, swap_q),
            "plan_uses_prefix_index": int(plan_uses["baseline"][qid]),
            "plan_uses_composite_index": int(plan_uses["swap"][qid]),
            "notes": "",
        })

    notes = f"original_category={selected_round.sample_category};warmup={int(warmup)};repeats={int(repeats)};{selected_round.notes}"
    row = {
        "round_id": selected_round.round_id,
        "sample_category": "excluded_unstable" if unstable else selected_round.sample_category,
        "unstable_excluded": int(unstable),
        "unstable_reason": unstable_reason,
        "pr20c_whatif_rel_improvement": selected_round.pr20c_whatif_rel_improvement,
        "baseline_config": config_to_string(baseline_config),
        "swap_config": config_to_string(swap_config),
        "prefix_index": format_candidate_key(prefix_index),
        "composite_index": format_candidate_key(composite_index),
        "baseline_exec_ms_all": _json_float_list(totals["baseline"]),
        "swap_exec_ms_all": _json_float_list(totals["swap"]),
        "baseline_exec_ms_median": baseline_median,
        "swap_exec_ms_median": swap_median,
        "baseline_exec_ms_mean": mean(totals["baseline"]),
        "swap_exec_ms_mean": mean(totals["swap"]),
        "baseline_exec_ms_stdev": stdev(totals["baseline"]),
        "swap_exec_ms_stdev": stdev(totals["swap"]),
        "baseline_cv": base_cv,
        "swap_cv": swap_cv,
        "run_order_id": run_order_id,
        "run_order": _json_list(order),
        "real_exec_delta_ms": real_delta,
        "real_exec_rel_improvement": real_rel,
        "outcome": outcome,
        "plan_uses_prefix_count": sum(1 for x in plan_uses["baseline"] if x),
        "plan_uses_composite_count": sum(1 for x in plan_uses["swap"] if x),
        "query_level_concentration": query_concentration(query_deltas),
        "num_queries": len(workload),
        "notes": notes,
    }
    excluded = None
    if unstable:
        excluded = {
            "round_id": selected_round.round_id,
            "original_sample_category": selected_round.sample_category,
            "unstable_reason": unstable_reason,
            "baseline_cv": base_cv,
            "swap_cv": swap_cv,
            "baseline_exec_ms_all": _json_float_list(totals["baseline"]),
            "swap_exec_ms_all": _json_float_list(totals["swap"]),
            "pr20c_whatif_rel_improvement": selected_round.pr20c_whatif_rel_improvement,
            "notes": notes,
        }
    return row, query_rows, excluded


def _safe_stats(values: Sequence[float]) -> Tuple[float, float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    return mean(values), median(values), min(values), max(values)


def _avg_ranks(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(indexed):
        end = pos + 1
        while end < len(indexed) and indexed[end][1] == indexed[pos][1]:
            end += 1
        avg = (pos + 1 + end) / 2.0
        for idx, _value in indexed[pos:end]:
            ranks[idx] = avg
        pos = end
    return ranks


def spearman_rank(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    rx = _avg_ranks(xs)
    ry = _avg_ranks(ys)
    mx = mean(rx)
    my = mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den_x = math.sqrt(sum((a - mx) ** 2 for a in rx))
    den_y = math.sqrt(sum((b - my) ** 2 for b in ry))
    if den_x <= 0.0 or den_y <= 0.0:
        return None
    return num / (den_x * den_y)


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


def sign_agreement_rate(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    if len(xs) != len(ys) or not xs:
        return None
    return sum(1 for a, b in zip(xs, ys) if _sign(a) == _sign(b)) / len(xs)


def summarize_results(
    round_rows: Sequence[Mapping[str, object]],
    excluded_rows: Sequence[Mapping[str, object]],
) -> List[Mapping[str, object]]:
    primary = [row for row in round_rows if not int(row.get("unstable_excluded", 0) or 0)]
    rows: List[Mapping[str, object]] = []
    for category in ("top_win", "mid_win", "low_win", "control"):
        cat_rows = [row for row in primary if row.get("sample_category") == category]
        rels = [float(row.get("real_exec_rel_improvement", 0.0) or 0.0) for row in cat_rows]
        m, med, mn, mx = _safe_stats(rels)
        rows.append({
            "row_type": "sample_category",
            "sample_category": category,
            "round_count": len(cat_rows),
            "mean_real_exec_rel_improvement": m,
            "median_real_exec_rel_improvement": med,
            "min_real_exec_rel_improvement": mn,
            "max_real_exec_rel_improvement": mx,
            "improved_count": sum(1 for row in cat_rows if row.get("outcome") == "improved"),
            "worse_count": sum(1 for row in cat_rows if row.get("outcome") == "worse"),
            "flat_count": sum(1 for row in cat_rows if row.get("outcome") == "flat"),
        })

    reason_counts: Dict[str, int] = {}
    for row in excluded_rows:
        reason = str(row.get("unstable_reason", "") or "unknown")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    rows.append({
        "row_type": "excluded_unstable",
        "sample_category": "excluded_unstable",
        "excluded_round_count": len(excluded_rows),
        "excluded_round_ids": ";".join(str(row.get("round_id")) for row in excluded_rows),
        "excluded_reason_counts": json.dumps(reason_counts, sort_keys=True, separators=(",", ":")),
        "baseline_cv_summary": _json_float_list([float(row.get("baseline_cv", 0.0) or 0.0) for row in excluded_rows]),
        "swap_cv_summary": _json_float_list([float(row.get("swap_cv", 0.0) or 0.0) for row in excluded_rows]),
    })

    whatif = [float(row.get("pr20c_whatif_rel_improvement", 0.0) or 0.0) for row in primary]
    real = [float(row.get("real_exec_rel_improvement", 0.0) or 0.0) for row in primary]
    sp = spearman_rank(whatif, real)
    agree = sign_agreement_rate(whatif, real)
    rows.append({
        "row_type": "descriptive_ordering",
        "sample_category": "all_primary",
        "round_count": len(primary),
        "spearman_rank_correlation": "" if sp is None else sp,
        "sign_agreement_rate": "" if agree is None else agree,
        "ordering_diagnostic_label": DESCRIPTIVE_ONLY_LABEL,
        "notes": "Do the orderings agree?",
    })
    return rows


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _fmt(row.get(col, "")) for col in columns})
    return path


def run_experiment(
    *,
    db,
    workloads: Sequence[Sequence[str]],
    metrics_csv: Path,
    pr20c_rounds_csv: Path,
    pr20c_candidates_csv: Path,
    output_root: Path,
    prefix_index: IndexKey,
    composite_index: IndexKey,
    max_num: int,
    selection_mode: str,
    max_rounds: int,
    warmup: int,
    repeats: int,
    max_cv: float,
    outcome_threshold: float,
    run_label: str,
    run_order_id: str,
) -> Tuple[Path, Path, Path, Path]:
    baselines = read_executed_configs(metrics_csv)
    selected = select_broader_rounds(
        rounds_csv=pr20c_rounds_csv,
        candidates_csv=pr20c_candidates_csv,
        composite_index=composite_index,
        selection_mode=selection_mode,
        max_rounds=max_rounds,
    )
    if not selected:
        raise RuntimeError("no target movie_info prefix-swap rounds selected")

    round_rows: List[Mapping[str, object]] = []
    query_rows: List[Mapping[str, object]] = []
    excluded_rows: List[Mapping[str, object]] = []
    for item in selected:
        if item.round_id < 0 or item.round_id >= len(workloads):
            raise IndexError(f"round_id={item.round_id} missing from loaded workloads")
        baseline = baselines.get(item.round_id)
        if baseline is None:
            raise KeyError(f"round_id={item.round_id} missing executed baseline config")
        round_row, qrows, excluded = evaluate_round(
            db=db,
            selected_round=item,
            workload=workloads[item.round_id],
            baseline_config=baseline,
            prefix_index=prefix_index,
            composite_index=composite_index,
            max_num=max_num,
            warmup=warmup,
            repeats=repeats,
            max_cv=max_cv,
            outcome_threshold=outcome_threshold,
            run_label=run_label,
            run_order_id=run_order_id,
        )
        round_rows.append(round_row)
        query_rows.extend(qrows)
        if excluded is not None:
            excluded_rows.append(excluded)

    output_root = Path(output_root)
    summary_path = output_root / "pr20e_broader_replay_summary.csv"
    rounds_path = output_root / "pr20e_broader_replay_rounds.csv"
    queries_path = output_root / "pr20e_broader_replay_queries.csv"
    excluded_path = output_root / "pr20e_broader_replay_excluded_unstable.csv"
    write_csv(summary_path, SUMMARY_COLUMNS, summarize_results(round_rows, excluded_rows))
    write_csv(rounds_path, ROUND_COLUMNS, round_rows)
    write_csv(queries_path, QUERY_COLUMNS, query_rows)
    write_csv(excluded_path, EXCLUDED_COLUMNS, excluded_rows)
    return summary_path, rounds_path, queries_path, excluded_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PR20e broader prefix-swap replay diagnostic.")
    p.add_argument("--benchmark", default="job")
    p.add_argument("--workload-type", default="random")
    p.add_argument("--round-size", type=int, default=33)
    p.add_argument("--database", required=True, help="Explicit scratch/local benchmark database name.")
    p.add_argument("--metrics-csv", type=Path, required=True)
    p.add_argument("--pr20c-rounds-csv", type=Path, required=True)
    p.add_argument("--pr20c-candidates-csv", type=Path, required=True)
    p.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    p.add_argument("--prefix-index", default=DEFAULT_PREFIX_INDEX)
    p.add_argument("--composite-index", default=DEFAULT_COMPOSITE_INDEX)
    p.add_argument("--max-num", type=int, default=10)
    p.add_argument("--selection-mode", choices=("all", "stratified"), default="all")
    p.add_argument("--max-rounds", type=int, default=0)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--max-cv", type=float, default=DEFAULT_MAX_CV)
    p.add_argument("--outcome-threshold", type=float, default=OUTCOME_THRESHOLD)
    p.add_argument("--run-label", default="job_pr20e_prefix_swap")
    p.add_argument("--run-order-id", default="alternating_pairs")
    p.add_argument("--experimental-physical-indexes", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    ensure_experimental_allowed(
        experimental_physical_indexes=bool(args.experimental_physical_indexes),
        database=str(args.database),
    )
    if int(args.warmup) < 1:
        raise ValueError("PR20e requires --warmup >= 1")
    if int(args.repeats) < 3:
        raise ValueError("PR20e requires --repeats >= 3")

    from database.database_connector import DatabaseConnector

    workloads = load_workloads(args.benchmark, args.workload_type, int(args.round_size))
    db = DatabaseConnector(str(args.database), virtual=False, run_num=1)
    try:
        paths = run_experiment(
            db=db,
            workloads=workloads,
            metrics_csv=Path(args.metrics_csv),
            pr20c_rounds_csv=Path(args.pr20c_rounds_csv),
            pr20c_candidates_csv=Path(args.pr20c_candidates_csv),
            output_root=Path(args.output_root),
            prefix_index=normalize_candidate_key(args.prefix_index),
            composite_index=normalize_candidate_key(args.composite_index),
            max_num=int(args.max_num),
            selection_mode=str(args.selection_mode),
            max_rounds=int(args.max_rounds),
            warmup=int(args.warmup),
            repeats=int(args.repeats),
            max_cv=float(args.max_cv),
            outcome_threshold=float(args.outcome_threshold),
            run_label=str(args.run_label),
            run_order_id=str(args.run_order_id),
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
