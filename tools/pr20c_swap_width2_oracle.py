#!/usr/bin/env python3
"""PR20c offline width-2 add/swap oracle.

This diagnostic consumes an existing AdaSelectPP metrics CSV and trace CSV. It
does not import AdaSelect, does not invoke candidate generation, and never
creates physical indexes. All cost checks are performed with the existing
HypoPG/what-if path through a virtual DatabaseConnector.
"""

from __future__ import annotations

import argparse
import ast
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.pr19_candidate_pool_common import IndexKey, format_candidate_key, normalize_candidate_key


NONTRIVIAL_THRESHOLD = 0.005

CANDIDATE_COLUMNS = [
    "benchmark",
    "workload_type",
    "round_id",
    "width2_index",
    "table",
    "columns",
    "baseline_config",
    "baseline_cost",
    "add_config",
    "add_cost",
    "add_delta",
    "add_relative_improvement",
    "add_feasible",
    "add_infeasible_reason",
    "swap_prefix_index",
    "swap_config",
    "swap_cost",
    "swap_delta",
    "swap_relative_improvement",
    "swap_feasible",
    "swap_infeasible_reason",
    "best_mode",
    "oracle_pass_add",
    "oracle_pass_swap",
]

ROUND_COLUMNS = [
    "round_id",
    "num_width2_candidates_tested",
    "num_add_feasible",
    "num_swap_feasible",
    "best_add_delta",
    "best_swap_delta",
    "best_add_relative_improvement",
    "best_swap_relative_improvement",
    "add_oracle_win",
    "swap_oracle_win",
    "best_add_index",
    "best_swap_index",
]

SUMMARY_COLUMNS = [
    "rounds",
    "tested_width2_candidates",
    "add_win_rounds",
    "swap_win_rounds",
    "mean_best_add_relative_improvement",
    "mean_best_swap_relative_improvement",
    "max_best_add_relative_improvement",
    "max_best_swap_relative_improvement",
    "conclusion",
]


def parse_config_repr(text: object) -> Set[IndexKey]:
    """Parse MetricsRecorder's repr(list[(table, cols)]) config field."""
    raw = str(text or "").strip()
    if not raw:
        return set()
    try:
        parsed = ast.literal_eval(raw)
    except Exception as exc:
        raise ValueError(f"invalid config repr: {raw!r}") from exc
    if not isinstance(parsed, (list, tuple, set)):
        raise ValueError(f"config repr is not a sequence: {raw!r}")
    return {normalize_candidate_key(item) for item in parsed}


def config_to_string(config: Iterable[IndexKey]) -> str:
    return ";".join(format_candidate_key(idx) for idx in sorted(set(config), key=format_candidate_key))


def width2_prefix_candidates(width2: IndexKey) -> Tuple[IndexKey, IndexKey]:
    table, cols = normalize_candidate_key(width2)
    if len(cols) != 2:
        raise ValueError(f"not a width-2 index: {format_candidate_key(width2)}")
    return (table, (cols[0],)), (table, (cols[1],))


def build_add_config(
    baseline_config: Set[IndexKey],
    width2: IndexKey,
    *,
    max_num: int,
) -> Tuple[Optional[Set[IndexKey]], bool, str]:
    """Return baseline + width2 if it fits capacity."""
    baseline = set(baseline_config or set())
    candidate = normalize_candidate_key(width2)
    if candidate in baseline:
        return baseline, True, ""
    if len(baseline) >= int(max_num):
        return None, False, "add_infeasible_due_to_capacity"
    out = set(baseline)
    out.add(candidate)
    return out, True, ""


def build_swap_config(
    baseline_config: Set[IndexKey],
    width2: IndexKey,
    *,
    max_num: int,
) -> Tuple[Optional[Set[IndexKey]], Optional[IndexKey], bool, str]:
    """Atomically replace a selected width-1 prefix with width2."""
    baseline = set(baseline_config or set())
    candidate = normalize_candidate_key(width2)
    left_prefix, right_prefix = width2_prefix_candidates(candidate)
    prefix = left_prefix if left_prefix in baseline else right_prefix if right_prefix in baseline else None
    if prefix is None:
        return None, None, False, "swap_infeasible_no_selected_prefix"
    out = set(baseline)
    out.discard(prefix)
    out.add(candidate)
    if len(out) > int(max_num):
        return None, prefix, False, "swap_infeasible_due_to_capacity"
    return out, prefix, True, ""


def relative_improvement(base_cost: float, new_cost: Optional[float]) -> float:
    if new_cost is None or abs(float(base_cost)) <= 1e-12:
        return 0.0
    return (float(base_cost) - float(new_cost)) / float(base_cost)


def _best_mode(
    *,
    baseline_cost: float,
    add_cost: Optional[float],
    add_feasible: bool,
    swap_cost: Optional[float],
    swap_feasible: bool,
) -> str:
    choices: List[Tuple[float, str]] = [(float(baseline_cost), "baseline")]
    if add_feasible and add_cost is not None:
        choices.append((float(add_cost), "add"))
    if swap_feasible and swap_cost is not None:
        choices.append((float(swap_cost), "swap"))
    return min(choices, key=lambda item: (item[0], item[1]))[1]


@dataclass(frozen=True)
class RoundInputs:
    round_id: int
    workload: Sequence[str]
    baseline_config: Set[IndexKey]
    width2_candidates: Set[IndexKey]


EvaluateConfig = Callable[[Sequence[str], Set[IndexKey]], float]


def evaluate_round_candidates(
    *,
    benchmark: str,
    workload_type: str,
    round_inputs: RoundInputs,
    evaluate_config: EvaluateConfig,
    max_num: int,
    threshold: float = NONTRIVIAL_THRESHOLD,
) -> List[Dict[str, object]]:
    """Evaluate baseline/add/swap families for one round."""
    baseline = set(round_inputs.baseline_config or set())
    baseline_cost = float(evaluate_config(round_inputs.workload, baseline))
    rows: List[Dict[str, object]] = []

    for width2 in sorted(round_inputs.width2_candidates, key=format_candidate_key):
        add_config, add_feasible, add_reason = build_add_config(baseline, width2, max_num=max_num)
        swap_config, swap_prefix, swap_feasible, swap_reason = build_swap_config(baseline, width2, max_num=max_num)

        add_cost: Optional[float] = None
        if add_feasible and add_config is not None:
            add_cost = float(evaluate_config(round_inputs.workload, add_config))

        swap_cost: Optional[float] = None
        if swap_feasible and swap_config is not None:
            swap_cost = float(evaluate_config(round_inputs.workload, swap_config))

        add_delta = float(baseline_cost) - float(add_cost) if add_cost is not None else 0.0
        swap_delta = float(baseline_cost) - float(swap_cost) if swap_cost is not None else 0.0
        add_rel = relative_improvement(baseline_cost, add_cost)
        swap_rel = relative_improvement(baseline_cost, swap_cost)

        rows.append({
            "benchmark": benchmark,
            "workload_type": workload_type,
            "round_id": int(round_inputs.round_id),
            "width2_index": format_candidate_key(width2),
            "table": width2[0],
            "columns": ",".join(width2[1]),
            "baseline_config": config_to_string(baseline),
            "baseline_cost": float(baseline_cost),
            "add_config": config_to_string(add_config or set()),
            "add_cost": add_cost if add_cost is not None else "",
            "add_delta": float(add_delta),
            "add_relative_improvement": float(add_rel),
            "add_feasible": int(bool(add_feasible)),
            "add_infeasible_reason": add_reason,
            "swap_prefix_index": format_candidate_key(swap_prefix) if swap_prefix else "",
            "swap_config": config_to_string(swap_config or set()),
            "swap_cost": swap_cost if swap_cost is not None else "",
            "swap_delta": float(swap_delta),
            "swap_relative_improvement": float(swap_rel),
            "swap_feasible": int(bool(swap_feasible)),
            "swap_infeasible_reason": swap_reason,
            "best_mode": _best_mode(
                baseline_cost=baseline_cost,
                add_cost=add_cost,
                add_feasible=add_feasible,
                swap_cost=swap_cost,
                swap_feasible=swap_feasible,
            ),
            "oracle_pass_add": int(bool(add_feasible) and add_rel >= float(threshold)),
            "oracle_pass_swap": int(bool(swap_feasible) and swap_rel >= float(threshold)),
        })
    return rows


def summarize_round(candidate_rows: Sequence[Mapping[str, object]], round_id: int, threshold: float) -> Dict[str, object]:
    add_feasible = [row for row in candidate_rows if int(row.get("add_feasible", 0) or 0)]
    swap_feasible = [row for row in candidate_rows if int(row.get("swap_feasible", 0) or 0)]
    best_add = (
        max(
            ((float(row.get("add_relative_improvement", 0.0) or 0.0), str(row.get("width2_index", "")), row) for row in add_feasible),
            key=lambda item: (item[0], item[1]),
        )
        if add_feasible
        else (0.0, "", None)
    )
    best_swap = (
        max(
            ((float(row.get("swap_relative_improvement", 0.0) or 0.0), str(row.get("width2_index", "")), row) for row in swap_feasible),
            key=lambda item: (item[0], item[1]),
        )
        if swap_feasible
        else (0.0, "", None)
    )
    best_add_row = best_add[2]
    best_swap_row = best_swap[2]
    return {
        "round_id": int(round_id),
        "num_width2_candidates_tested": int(len(candidate_rows)),
        "num_add_feasible": int(len(add_feasible)),
        "num_swap_feasible": int(len(swap_feasible)),
        "best_add_delta": float(best_add_row.get("add_delta", 0.0) or 0.0) if best_add_row else 0.0,
        "best_swap_delta": float(best_swap_row.get("swap_delta", 0.0) or 0.0) if best_swap_row else 0.0,
        "best_add_relative_improvement": float(best_add[0]),
        "best_swap_relative_improvement": float(best_swap[0]),
        "add_oracle_win": int(float(best_add[0]) >= float(threshold)),
        "swap_oracle_win": int(float(best_swap[0]) >= float(threshold)),
        "best_add_index": best_add_row.get("width2_index", "") if best_add_row else "",
        "best_swap_index": best_swap_row.get("width2_index", "") if best_swap_row else "",
    }


def summarize_overall(round_rows: Sequence[Mapping[str, object]], threshold: float) -> Dict[str, object]:
    rounds = len(round_rows)
    add_win_rounds = sum(int(row.get("add_oracle_win", 0) or 0) for row in round_rows)
    swap_win_rounds = sum(int(row.get("swap_oracle_win", 0) or 0) for row in round_rows)
    tested = sum(int(row.get("num_width2_candidates_tested", 0) or 0) for row in round_rows)
    add_rels = [float(row.get("best_add_relative_improvement", 0.0) or 0.0) for row in round_rows]
    swap_rels = [float(row.get("best_swap_relative_improvement", 0.0) or 0.0) for row in round_rows]
    mean_add = sum(add_rels) / float(rounds) if rounds else 0.0
    mean_swap = sum(swap_rels) / float(rounds) if rounds else 0.0
    max_add = max(add_rels) if add_rels else 0.0
    max_swap = max(swap_rels) if swap_rels else 0.0

    if swap_win_rounds > 0 and (mean_swap >= threshold or max_swap >= threshold):
        if add_win_rounds == 0:
            conclusion = "width-2 value is primarily replacement value; selector-level retain/swap is needed."
        else:
            conclusion = "width-2 has replacement value; current selector likely cannot express or rank the swap."
    elif add_win_rounds > 0:
        conclusion = "width-2 has additive value; investigate eval/ranking coverage."
    else:
        conclusion = "JOB width-2 appears low-value under this split; do not pursue PR21 for JOB width-2 yet."

    return {
        "rounds": int(rounds),
        "tested_width2_candidates": int(tested),
        "add_win_rounds": int(add_win_rounds),
        "swap_win_rounds": int(swap_win_rounds),
        "mean_best_add_relative_improvement": float(mean_add),
        "mean_best_swap_relative_improvement": float(mean_swap),
        "max_best_add_relative_improvement": float(max_add),
        "max_best_swap_relative_improvement": float(max_swap),
        "conclusion": conclusion,
    }


def _read_metrics_baselines(path: Path) -> Dict[int, Set[IndexKey]]:
    rows: Dict[int, Set[IndexKey]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rid_text = str(row.get("round", "")).strip()
            if not rid_text or rid_text.upper() == "SUMMARY":
                continue
            rid = int(float(rid_text))
            rows[rid] = parse_config_repr(row.get("new", ""))
    return rows


def _read_trace_width2_appearing(path: Path) -> Dict[int, Set[IndexKey]]:
    rows: Dict[int, Set[IndexKey]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if str(row.get("in_appearing", "")).strip() not in {"1", "1.0", "true", "True"}:
                continue
            table = str(row.get("table", "")).strip()
            cols = tuple(c.strip() for c in str(row.get("cols", "")).split(",") if c.strip())
            if not table or len(cols) != 2:
                continue
            rid = int(float(str(row.get("round", "0")).strip() or 0))
            rows.setdefault(rid, set()).add(normalize_candidate_key((table, cols)))
    return rows


def _evaluate_config_with_hypopg(db_con, cost_eval, workload: Sequence[str], indexes: Set[IndexKey]) -> float:
    db_con.drop_all_indexes()
    for table, cols in sorted(set(indexes), key=format_candidate_key):
        db_con.create_index(table, cols)
    try:
        return float(cost_eval.calculate_now_cost(list(workload)))
    finally:
        db_con.drop_all_indexes()


def _fmt(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.12g}"
    return value


def write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _fmt(row.get(col, "")) for col in columns})
    return path


def run_oracle(
    *,
    benchmark: str,
    workload_type: str,
    workloads: Sequence[Sequence[str]],
    metrics_csv: Path,
    trace_csv: Path,
    output_root: Path,
    max_num: int,
    threshold: float,
    evaluate_config: EvaluateConfig,
) -> Tuple[Path, Path, Path]:
    baselines = _read_metrics_baselines(metrics_csv)
    width2_by_round = _read_trace_width2_appearing(trace_csv)

    candidate_rows: List[Dict[str, object]] = []
    round_rows: List[Dict[str, object]] = []
    round_ids = sorted(set(baselines))
    for rid in round_ids:
        if rid < 0 or rid >= len(workloads):
            raise IndexError(f"round_id={rid} missing from loaded workloads")
        width2_candidates = width2_by_round.get(rid, set())
        rows = []
        if width2_candidates:
            rows = evaluate_round_candidates(
                benchmark=benchmark,
                workload_type=workload_type,
                round_inputs=RoundInputs(
                    round_id=rid,
                    workload=workloads[rid],
                    baseline_config=baselines.get(rid, set()),
                    width2_candidates=width2_candidates,
                ),
                evaluate_config=evaluate_config,
                max_num=max_num,
                threshold=threshold,
            )
        candidate_rows.extend(rows)
        round_rows.append(summarize_round(rows, rid, threshold))

    summary_row = summarize_overall(round_rows, threshold)
    candidates_path = output_root / "pr20c_width2_oracle_candidates.csv"
    rounds_path = output_root / "pr20c_width2_oracle_rounds.csv"
    summary_path = output_root / "pr20c_width2_oracle_summary.csv"
    write_csv(candidates_path, CANDIDATE_COLUMNS, candidate_rows)
    write_csv(rounds_path, ROUND_COLUMNS, round_rows)
    write_csv(summary_path, SUMMARY_COLUMNS, [summary_row])
    return candidates_path, rounds_path, summary_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PR20c offline width-2 add/swap oracle.")
    p.add_argument("--benchmark", default="job")
    p.add_argument("--workload-type", default="random")
    p.add_argument("--round-size", type=int, default=33)
    p.add_argument("--metrics-csv", type=Path, required=True)
    p.add_argument("--trace-csv", type=Path, required=True)
    p.add_argument("--output-root", type=Path, default=Path("runs_pr20c_swap_width2_oracle"))
    p.add_argument("--max-num", type=int, default=10)
    p.add_argument("--threshold", type=float, default=NONTRIVIAL_THRESHOLD)
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    from adasel.main import load_workloads
    from database.cost_evaluation import CostEvaluation
    from database.database_connector import DatabaseConnector

    workloads = load_workloads(args.benchmark, args.workload_type, int(args.round_size))
    db = DatabaseConnector(args.benchmark, virtual=True, run_num=1)
    cost_eval = CostEvaluation(db, args.benchmark, cuda=False)
    try:
        paths = run_oracle(
            benchmark=args.benchmark,
            workload_type=args.workload_type,
            workloads=workloads,
            metrics_csv=Path(args.metrics_csv),
            trace_csv=Path(args.trace_csv),
            output_root=Path(args.output_root),
            max_num=int(args.max_num),
            threshold=float(args.threshold),
            evaluate_config=lambda workload, indexes: _evaluate_config_with_hypopg(db, cost_eval, workload, indexes),
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
