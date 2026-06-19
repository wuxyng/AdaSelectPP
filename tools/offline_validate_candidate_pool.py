#!/usr/bin/env python3
"""PR19 pool-restricted offline CELF oracle.

This validator consumes only exported candidate pools. It does not import or
invoke AdaSelectPP candidate generation, and it never creates physical indexes.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.pr19_candidate_pool_common import (
    LITESELECT_TWOCELF_IMPORTED,
    MODES,
    SELECTOR_NAME,
    SELECTOR_SEMANTICS,
    IndexKey,
    format_candidate_key,
    parse_candidate_string,
)


CSV_COLUMNS = [
    "bench",
    "workload_type",
    "round_id",
    "mode",
    "selector_name",
    "selector_semantics",
    "liteselect_twocelf_imported",
    "pool_size",
    "width1_count",
    "width2_count",
    "selected_count",
    "selected_width1",
    "selected_width2",
    "selected_indexes",
    "base_cost",
    "selected_cost",
    "relative_improvement",
    "whatif_calls",
    "selector_time_ms",
]


def _read_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_candidate_pool(row: Dict) -> Set[IndexKey]:
    return {parse_candidate_string(text) for text in row.get("candidates", []) or []}


def _evaluate_config(
    *,
    db_con: DatabaseConnector,
    cost_eval: CostEvaluation,
    workload: Sequence[str],
    indexes: Iterable[IndexKey],
) -> float:
    db_con.drop_all_indexes()
    for table, cols in sorted({idx for idx in indexes}, key=format_candidate_key):
        db_con.create_index(table, cols)
    try:
        return float(cost_eval.calculate_now_cost(list(workload)))
    finally:
        db_con.drop_all_indexes()


def select_with_celf(
    *,
    candidates: Iterable[IndexKey],
    workload: Sequence[str],
    db_con: DatabaseConnector,
    cost_eval: CostEvaluation,
    base_cost: float,
    max_num: int,
) -> Tuple[Set[IndexKey], float, int]:
    """Deterministic CELF-style greedy selection over one exported pool."""
    ordered = sorted({idx for idx in candidates}, key=format_candidate_key)
    selected: Set[IndexKey] = set()
    current_cost = float(base_cost)
    whatif_calls = 0
    version = 0
    heap: List[Tuple[float, str, int, float, IndexKey]] = []
    query_count = len(list(workload))

    for idx in ordered:
        cost = _evaluate_config(db_con=db_con, cost_eval=cost_eval, workload=workload, indexes={idx})
        whatif_calls += query_count
        gain = float(base_cost) - float(cost)
        heapq.heappush(heap, (-gain, format_candidate_key(idx), 0, float(cost), idx))

    while heap and len(selected) < int(max_num):
        neg_gain, _name, item_version, cached_cost, idx = heapq.heappop(heap)
        if idx in selected:
            continue
        gain = -float(neg_gain)
        if item_version == version:
            if gain <= 1e-9:
                break
            selected.add(idx)
            current_cost = float(cached_cost)
            version += 1
            continue
        cost = _evaluate_config(
            db_con=db_con,
            cost_eval=cost_eval,
            workload=workload,
            indexes=set(selected) | {idx},
        )
        whatif_calls += query_count
        gain = current_cost - float(cost)
        heapq.heappush(heap, (-gain, format_candidate_key(idx), version, float(cost), idx))

    return selected, float(current_cost), int(whatif_calls)


def validate_row(
    *,
    row: Dict,
    workload: Sequence[str],
    db_con: DatabaseConnector,
    cost_eval: CostEvaluation,
    max_num: int,
) -> Dict[str, object]:
    candidates = _load_candidate_pool(row)
    base_cost = _evaluate_config(db_con=db_con, cost_eval=cost_eval, workload=workload, indexes=set())
    start = time.perf_counter()
    selected, selected_cost, whatif_calls = select_with_celf(
        candidates=candidates,
        workload=workload,
        db_con=db_con,
        cost_eval=cost_eval,
        base_cost=base_cost,
        max_num=max_num,
    )
    selector_time_ms = (time.perf_counter() - start) * 1000.0
    selected_strings = sorted(format_candidate_key(idx) for idx in selected)
    relative_improvement = 0.0
    if abs(float(base_cost)) > 1e-12:
        relative_improvement = (float(base_cost) - float(selected_cost)) / float(base_cost)
    return {
        "bench": row.get("bench", ""),
        "workload_type": row.get("workload_type", ""),
        "round_id": int(row.get("round_id", 0)),
        "mode": row.get("mode", ""),
        "selector_name": SELECTOR_NAME,
        "selector_semantics": SELECTOR_SEMANTICS,
        "liteselect_twocelf_imported": LITESELECT_TWOCELF_IMPORTED,
        "pool_size": int(len(candidates)),
        "width1_count": int(sum(1 for idx in candidates if len(idx[1]) == 1)),
        "width2_count": int(sum(1 for idx in candidates if len(idx[1]) == 2)),
        "selected_count": int(len(selected)),
        "selected_width1": int(sum(1 for idx in selected if len(idx[1]) == 1)),
        "selected_width2": int(sum(1 for idx in selected if len(idx[1]) == 2)),
        "selected_indexes": ";".join(selected_strings),
        "base_cost": float(base_cost),
        "selected_cost": float(selected_cost),
        "relative_improvement": float(relative_improvement),
        "whatif_calls": int(whatif_calls),
        "selector_time_ms": float(selector_time_ms),
    }


def validate_candidate_pool_file(
    *,
    pool_path: Path,
    workloads: Sequence[Sequence[str]],
    db_con: DatabaseConnector,
    cost_eval: CostEvaluation,
    max_num: int,
    output_path: Path,
) -> Path:
    rows = _read_jsonl(pool_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            round_id = int(row.get("round_id", 0))
            if round_id < 0 or round_id >= len(workloads):
                raise IndexError(f"round_id={round_id} missing from loaded workloads")
            writer.writerow(
                validate_row(
                    row=row,
                    workload=workloads[round_id],
                    db_con=db_con,
                    cost_eval=cost_eval,
                    max_num=max_num,
                )
            )
    return output_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate exported PR19 candidate pools with offline_pool_celf.")
    p.add_argument("bench")
    p.add_argument("workload_type")
    p.add_argument("--round-size", type=int, default=50)
    p.add_argument("--max-num", type=int, default=10)
    p.add_argument("--output-root", type=Path, default=Path("runs_pr19_candidate_pool"))
    p.add_argument("--mode", choices=["both", *MODES], default="both")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    from adasel.main import load_workloads
    from database.cost_evaluation import CostEvaluation
    from database.database_connector import DatabaseConnector

    modes = MODES if args.mode == "both" else (args.mode,)
    workloads = load_workloads(args.bench, args.workload_type, int(args.round_size))
    db = DatabaseConnector(args.bench, virtual=True, run_num=1)
    cost_eval = CostEvaluation(db, args.bench, cuda=False)
    try:
        for mode in modes:
            mode_dir = Path(args.output_root) / f"{args.bench}_{args.workload_type}" / mode
            out = validate_candidate_pool_file(
                pool_path=mode_dir / "candidate_pools.jsonl",
                workloads=workloads,
                db_con=db,
                cost_eval=cost_eval,
                max_num=int(args.max_num),
                output_path=mode_dir / "offline_validation.csv",
            )
            print(f"{mode}: {out}")
    finally:
        try:
            db.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
