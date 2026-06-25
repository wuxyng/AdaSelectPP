#!/usr/bin/env python3
"""PR20d offline real-execution prefix-swap validation.

This tool is an offline experimental diagnostic. It materializes deterministic
physical indexes only when explicitly allowed by --experimental-physical-indexes
and compares a baseline AdaSelectPP configuration against one target prefix
swap. It does not import AdaSelect, candidate generation, selector policy, or
materialization policy.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adaselect_pp.common import canonical_workload_line, sql_only
from tools.pr19_candidate_pool_common import IndexKey, format_candidate_key, normalize_candidate_key
from tools.pr20c_swap_width2_oracle import parse_config_repr, relative_improvement


DEFAULT_PREFIX_INDEX = "movie_info(mi_movie_id)"
DEFAULT_COMPOSITE_INDEX = "movie_info(mi_movie_id,mi_info_type_id)"
DEFAULT_THRESHOLD = 0.01

ROUND_COLUMNS = [
    "round_id",
    "round_role",
    "baseline_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "pr20c_swap_relative_improvement",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_relative_improvement",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "num_queries",
    "prefix_plan_used_query_count",
    "composite_plan_used_query_count",
    "positive_query_count",
    "top_query_delta_ms",
    "top_query_delta_share",
    "notes",
]

QUERY_COLUMNS = [
    "round_id",
    "query_id",
    "baseline_exec_ms_median",
    "swap_exec_ms_median",
    "exec_delta_ms",
    "exec_relative_improvement",
    "plan_uses_prefix_index",
    "plan_uses_composite_index",
    "notes",
]

SUMMARY_COLUMNS = [
    "rounds",
    "winning_rounds_tested",
    "control_rounds_tested",
    "improved_rounds_at_threshold",
    "flat_or_worse_rounds",
    "mean_exec_relative_improvement",
    "median_exec_relative_improvement",
    "max_exec_relative_improvement",
    "prefix_plan_used_query_count",
    "composite_plan_used_query_count",
    "mean_top_query_delta_share",
    "conclusion",
]


@dataclass(frozen=True)
class SelectedRound:
    round_id: int
    role: str
    pr20c_swap_relative_improvement: float
    notes: str = ""


@dataclass(frozen=True)
class IndexDDL:
    index: IndexKey
    name: str
    create_sql: str
    drop_sql: str


@dataclass
class ExecutionResult:
    total_ms_all: List[float]
    per_query_ms_all: List[List[float]]
    plan_uses_index: List[bool]


def load_workloads(bench: str, wtype: str, round_size: int, *, root: Path = ROOT) -> List[List[str]]:
    """Load workload rounds without importing the online AdaSelect driver."""
    path = root / "database" / "workload" / f"{bench}_{wtype}.txt"
    if not path.exists():
        raise FileNotFoundError(f"workload file not found: {path}")

    workloads: List[List[str]] = []
    cur: List[str] = []
    line_no = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip("\n")
            if not line:
                continue
            cur.append(canonical_workload_line(line, fallback_id=str(line_no)))
            line_no += 1
            if len(cur) >= int(round_size):
                workloads.append(cur)
                cur = []
    if cur:
        workloads.append(cur)
    return workloads


def read_executed_configs(metrics_csv: Path) -> Dict[int, Set[IndexKey]]:
    """Read the physical config that executed W_t from the metrics `old` column.

    In `adasel/main.py`, each round first executes W_t, then snapshots
    `old_conf`, then tunes and records `new_conf`. Therefore `old` is the
    applied physical configuration for W_t; `new` is the recommendation applied
    after W_t for the next round.
    """
    configs: Dict[int, Set[IndexKey]] = {}
    with Path(metrics_csv).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rid_text = str(row.get("round", "")).strip()
            if not rid_text or rid_text.upper() == "SUMMARY":
                continue
            configs[int(float(rid_text))] = parse_config_repr(row.get("old", ""))
    return configs


def read_pr20c_target_rows(candidates_csv: Path, composite_index: IndexKey) -> Dict[int, Mapping[str, str]]:
    target = format_candidate_key(composite_index)
    rows: Dict[int, Mapping[str, str]] = {}
    with Path(candidates_csv).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if str(row.get("width2_index", "")).strip() != target:
                continue
            rid = int(float(str(row.get("round_id", "0")).strip() or 0))
            rows[rid] = row
    return rows


def read_pr20c_round_rows(rounds_csv: Path) -> Dict[int, Mapping[str, str]]:
    rows: Dict[int, Mapping[str, str]] = {}
    with Path(rounds_csv).open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rid = int(float(str(row.get("round_id", "0")).strip() or 0))
            rows[rid] = row
    return rows


def _row_float(row: Mapping[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(str(row.get(key, "")).strip())
    except Exception:
        return default


def _row_true(row: Mapping[str, str], key: str) -> bool:
    return str(row.get(key, "")).strip().lower() in {"1", "1.0", "true", "yes"}


def select_rounds(
    *,
    rounds_csv: Path,
    candidates_csv: Path,
    composite_index: IndexKey,
    top_k: int,
    control_count: int,
    threshold: float,
) -> List[SelectedRound]:
    """Select top predicted target-pattern wins plus non-winning controls."""
    composite_name = format_candidate_key(composite_index)
    target_rows = read_pr20c_target_rows(candidates_csv, composite_index)
    round_rows = read_pr20c_round_rows(rounds_csv)

    primary: List[SelectedRound] = []
    for rid, row in round_rows.items():
        if str(row.get("best_swap_index", "")).strip() != composite_name:
            continue
        target_row = target_rows.get(rid, {})
        rel = _row_float(target_row, "swap_relative_improvement", _row_float(row, "best_swap_relative_improvement"))
        if rel >= float(threshold):
            primary.append(SelectedRound(rid, "predicted_win", rel, "best_swap_index_matches_target"))

    if not primary:
        for rid, row in target_rows.items():
            rel = _row_float(row, "swap_relative_improvement")
            if _row_true(row, "oracle_pass_swap") and rel >= float(threshold):
                primary.append(SelectedRound(rid, "predicted_win", rel, "target_candidate_oracle_pass"))

    primary.sort(key=lambda item: (-item.pr20c_swap_relative_improvement, item.round_id))
    selected = primary[: max(0, int(top_k))]
    selected_ids = {item.round_id for item in selected}

    controls: List[SelectedRound] = []
    for rid, row in target_rows.items():
        if rid in selected_ids:
            continue
        rel = _row_float(row, "swap_relative_improvement")
        if not _row_true(row, "oracle_pass_swap") or rel < float(threshold):
            controls.append(SelectedRound(rid, "control_non_win", rel, "target_candidate_below_threshold"))
    controls.sort(key=lambda item: (-item.pr20c_swap_relative_improvement, item.round_id))
    selected.extend(controls[: max(0, int(control_count))])
    return selected


def build_prefix_swap_config(
    baseline_config: Iterable[IndexKey],
    prefix_index: IndexKey,
    composite_index: IndexKey,
    *,
    max_num: int,
) -> Tuple[Optional[Set[IndexKey]], bool, str]:
    baseline = {normalize_candidate_key(idx) for idx in baseline_config}
    prefix = normalize_candidate_key(prefix_index)
    composite = normalize_candidate_key(composite_index)
    if prefix not in baseline:
        return None, False, "prefix_missing_from_baseline"
    out = set(baseline)
    out.remove(prefix)
    out.add(composite)
    if len(out) > int(max_num):
        return None, False, "swap_exceeds_max_num"
    return out, True, ""


def config_to_string(config: Iterable[IndexKey]) -> str:
    return ";".join(format_candidate_key(idx) for idx in sorted(set(config), key=format_candidate_key))


def ensure_config_capacity(config: Iterable[IndexKey], *, max_num: int) -> None:
    size = len(set(config))
    if size > int(max_num):
        raise ValueError(f"physical config has {size} indexes, exceeds max_num={max_num}")


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _safe_name_part(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", str(text).lower()).strip("_") or "x"


def deterministic_index_name(*, run_label: str, round_id: int, config_label: str, index: IndexKey) -> str:
    idx_text = format_candidate_key(index)
    digest = hashlib.md5(f"{run_label}|{round_id}|{config_label}|{idx_text}".encode("utf-8")).hexdigest()[:10]
    table, cols = normalize_candidate_key(index)
    body = _safe_name_part(f"{run_label}_{round_id}_{config_label}_{table}_{'_'.join(cols)}")
    prefix = "pr20d_"
    max_body = 63 - len(prefix) - len(digest) - 1
    return f"{prefix}{body[:max_body]}_{digest}"


def generate_index_ddls(
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
        name = deterministic_index_name(
            run_label=run_label,
            round_id=int(round_id),
            config_label=config_label,
            index=idx,
        )
        create_sql = f"CREATE INDEX {quote_ident(name)} ON {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)})"
        drop_sql = f"DROP INDEX IF EXISTS {quote_ident(name)}"
        ddls.append(IndexDDL(index=idx, name=name, create_sql=create_sql, drop_sql=drop_sql))
    return ddls


def ensure_experimental_allowed(*, experimental_physical_indexes: bool, database: str) -> None:
    if not experimental_physical_indexes:
        raise PermissionError(
            "PR20d creates physical experimental indexes; rerun with "
            "--experimental-physical-indexes and an explicit --database scratch DB."
        )
    if not str(database or "").strip():
        raise PermissionError("PR20d requires an explicit --database scratch DB name.")


def _execute_analyze_plan(db, query: str) -> Tuple[float, Mapping[str, object]]:
    sql = sql_only(query)
    row = db.execute_and_fetch(f"EXPLAIN (ANALYZE, FORMAT JSON) {sql}", True)
    plan_doc = row[0][0] if row and row[0] else {}
    plan = plan_doc.get("Plan", {}) if isinstance(plan_doc, Mapping) else {}
    runtime = float(plan.get("Actual Total Time", 0.0) or 0.0)
    return runtime, plan


def _iter_plan_nodes(plan: Mapping[str, object]):
    yield plan
    for child in plan.get("Plans", []) or []:
        if isinstance(child, Mapping):
            yield from _iter_plan_nodes(child)


def plan_uses_index(plan: Mapping[str, object], index_name: str) -> bool:
    target = str(index_name or "")
    for node in _iter_plan_nodes(plan):
        if str(node.get("Index Name", "")) == target:
            return True
    return False


def drop_config_indexes(db, ddls: Sequence[IndexDDL]) -> None:
    for ddl in reversed(list(ddls)):
        try:
            db.execute_only(ddl.drop_sql)
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


def materialize_config(db, ddls: Sequence[IndexDDL]) -> None:
    drop_config_indexes(db, ddls)
    for ddl in ddls:
        db.execute_only(ddl.create_sql)


def run_repeated_workload(
    db,
    workload: Sequence[str],
    *,
    watched_index_name: str,
    warmup: int,
    repeats: int,
) -> ExecutionResult:
    measured_totals: List[float] = []
    per_query: List[List[float]] = [[] for _ in workload]
    plan_uses = [False for _ in workload]

    total_runs = int(warmup) + int(repeats)
    for run_no in range(total_runs):
        round_total = 0.0
        measured = run_no >= int(warmup)
        for query_id, query in enumerate(workload):
            runtime_ms, plan = _execute_analyze_plan(db, query)
            uses_index = plan_uses_index(plan, watched_index_name)
            plan_uses[query_id] = plan_uses[query_id] or uses_index
            if measured:
                per_query[query_id].append(runtime_ms)
                round_total += runtime_ms
        if measured:
            measured_totals.append(round_total)
    return ExecutionResult(measured_totals, per_query, plan_uses)


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _json_float_list(values: Sequence[float]) -> str:
    return json.dumps([round(float(v), 6) for v in values])


def _fmt(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.12g}"
    return value


def _write_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _fmt(row.get(col, "")) for col in columns})
    return path


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
    run_label: str,
) -> Tuple[Mapping[str, object], List[Mapping[str, object]]]:
    swap_config, feasible, reason = build_prefix_swap_config(
        baseline_config,
        prefix_index,
        composite_index,
        max_num=max_num,
    )
    if not feasible or swap_config is None:
        notes = f"skipped:{reason}"
        row = {
            "round_id": selected_round.round_id,
            "round_role": selected_round.role,
            "baseline_config": config_to_string(baseline_config),
            "swap_config": "",
            "prefix_index": format_candidate_key(prefix_index),
            "composite_index": format_candidate_key(composite_index),
            "pr20c_swap_relative_improvement": selected_round.pr20c_swap_relative_improvement,
            "baseline_exec_ms_median": 0.0,
            "swap_exec_ms_median": 0.0,
            "exec_delta_ms": 0.0,
            "exec_relative_improvement": 0.0,
            "baseline_exec_ms_all": "[]",
            "swap_exec_ms_all": "[]",
            "num_queries": len(workload),
            "notes": notes,
        }
        return row, []

    baseline_ddls = generate_index_ddls(
        baseline_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="baseline",
        max_num=max_num,
    )
    swap_ddls = generate_index_ddls(
        swap_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="swap",
        max_num=max_num,
    )
    prefix_name = next((ddl.name for ddl in baseline_ddls if normalize_candidate_key(ddl.index) == normalize_candidate_key(prefix_index)), "")
    composite_name = next((ddl.name for ddl in swap_ddls if normalize_candidate_key(ddl.index) == normalize_candidate_key(composite_index)), "")

    try:
        materialize_config(db, baseline_ddls)
        baseline_result = run_repeated_workload(
            db,
            workload,
            watched_index_name=prefix_name,
            warmup=warmup,
            repeats=repeats,
        )
    finally:
        drop_config_indexes(db, baseline_ddls)

    try:
        materialize_config(db, swap_ddls)
        swap_result = run_repeated_workload(
            db,
            workload,
            watched_index_name=composite_name,
            warmup=warmup,
            repeats=repeats,
        )
    finally:
        drop_config_indexes(db, swap_ddls)

    baseline_total = _median(baseline_result.total_ms_all)
    swap_total = _median(swap_result.total_ms_all)
    exec_delta = baseline_total - swap_total
    exec_rel = relative_improvement(baseline_total, swap_total)

    query_rows: List[Mapping[str, object]] = []
    positive_deltas: List[float] = []
    for query_id, (base_times, swap_times) in enumerate(zip(baseline_result.per_query_ms_all, swap_result.per_query_ms_all)):
        base_median = _median(base_times)
        swap_median = _median(swap_times)
        delta = base_median - swap_median
        if delta > 0:
            positive_deltas.append(delta)
        query_rows.append({
            "round_id": selected_round.round_id,
            "query_id": query_id,
            "baseline_exec_ms_median": base_median,
            "swap_exec_ms_median": swap_median,
            "exec_delta_ms": delta,
            "exec_relative_improvement": relative_improvement(base_median, swap_median),
            "plan_uses_prefix_index": int(bool(baseline_result.plan_uses_index[query_id])),
            "plan_uses_composite_index": int(bool(swap_result.plan_uses_index[query_id])),
            "notes": "",
        })

    positive_sum = sum(positive_deltas)
    top_delta = max(positive_deltas) if positive_deltas else 0.0
    top_share = (top_delta / positive_sum) if positive_sum > 0 else 0.0
    round_notes = f"warmup={int(warmup)};repeats={int(repeats)};{selected_round.notes}"
    round_row = {
        "round_id": selected_round.round_id,
        "round_role": selected_round.role,
        "baseline_config": config_to_string(baseline_config),
        "swap_config": config_to_string(swap_config),
        "prefix_index": format_candidate_key(prefix_index),
        "composite_index": format_candidate_key(composite_index),
        "pr20c_swap_relative_improvement": selected_round.pr20c_swap_relative_improvement,
        "baseline_exec_ms_median": baseline_total,
        "swap_exec_ms_median": swap_total,
        "exec_delta_ms": exec_delta,
        "exec_relative_improvement": exec_rel,
        "baseline_exec_ms_all": _json_float_list(baseline_result.total_ms_all),
        "swap_exec_ms_all": _json_float_list(swap_result.total_ms_all),
        "num_queries": len(workload),
        "prefix_plan_used_query_count": sum(1 for x in baseline_result.plan_uses_index if x),
        "composite_plan_used_query_count": sum(1 for x in swap_result.plan_uses_index if x),
        "positive_query_count": len(positive_deltas),
        "top_query_delta_ms": top_delta,
        "top_query_delta_share": top_share,
        "notes": round_notes,
    }
    return round_row, query_rows


def summarize_rounds(round_rows: Sequence[Mapping[str, object]], *, threshold: float) -> Mapping[str, object]:
    rels = [float(row.get("exec_relative_improvement", 0.0) or 0.0) for row in round_rows]
    winning_rows = [row for row in round_rows if row.get("round_role") == "predicted_win"]
    improved_winning = [
        row for row in winning_rows
        if float(row.get("exec_relative_improvement", 0.0) or 0.0) >= float(threshold)
    ]
    improved_all = sum(1 for rel in rels if rel >= float(threshold))
    flat_or_worse = sum(1 for rel in rels if rel <= 0.0)

    if winning_rows and len(improved_winning) >= (len(winning_rows) + 1) // 2:
        conclusion = "PR20c what-if swap value is supported by real execution; selector-level prefix-swap is worth pursuing."
    elif len(improved_winning) == 1:
        conclusion = "evidence is promising but needs seed/split replication before PR21b."
    elif winning_rows:
        conclusion = "PR20c identified a what-if/cost-model gap; do not implement selector changes yet."
    else:
        conclusion = "no predicted winning rounds were tested; rerun with PR20c target rounds."

    top_shares = [float(row.get("top_query_delta_share", 0.0) or 0.0) for row in round_rows]
    return {
        "rounds": len(round_rows),
        "winning_rounds_tested": len(winning_rows),
        "control_rounds_tested": sum(1 for row in round_rows if row.get("round_role") == "control_non_win"),
        "improved_rounds_at_threshold": improved_all,
        "flat_or_worse_rounds": flat_or_worse,
        "mean_exec_relative_improvement": statistics.mean(rels) if rels else 0.0,
        "median_exec_relative_improvement": statistics.median(rels) if rels else 0.0,
        "max_exec_relative_improvement": max(rels) if rels else 0.0,
        "prefix_plan_used_query_count": sum(int(row.get("prefix_plan_used_query_count", 0) or 0) for row in round_rows),
        "composite_plan_used_query_count": sum(int(row.get("composite_plan_used_query_count", 0) or 0) for row in round_rows),
        "mean_top_query_delta_share": statistics.mean(top_shares) if top_shares else 0.0,
        "conclusion": conclusion,
    }


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
    top_k: int,
    control_count: int,
    warmup: int,
    repeats: int,
    threshold: float,
    run_label: str,
) -> Tuple[Path, Path, Path]:
    baselines = read_executed_configs(metrics_csv)
    selected = select_rounds(
        rounds_csv=pr20c_rounds_csv,
        candidates_csv=pr20c_candidates_csv,
        composite_index=composite_index,
        top_k=top_k,
        control_count=control_count,
        threshold=0.005,
    )
    if not selected:
        raise RuntimeError("no PR20c target rounds selected for PR20d")

    round_rows: List[Mapping[str, object]] = []
    query_rows: List[Mapping[str, object]] = []
    for item in selected:
        if item.round_id < 0 or item.round_id >= len(workloads):
            raise IndexError(f"round_id={item.round_id} missing from loaded workloads")
        baseline = baselines.get(item.round_id)
        if baseline is None:
            raise KeyError(f"round_id={item.round_id} missing baseline config")
        round_row, qrows = evaluate_round(
            db=db,
            selected_round=item,
            workload=workloads[item.round_id],
            baseline_config=baseline,
            prefix_index=prefix_index,
            composite_index=composite_index,
            max_num=max_num,
            warmup=warmup,
            repeats=repeats,
            run_label=run_label,
        )
        round_rows.append(round_row)
        query_rows.extend(qrows)

    summary = summarize_rounds(round_rows, threshold=threshold)
    output_root = Path(output_root)
    summary_path = output_root / "pr20d_real_exec_summary.csv"
    rounds_path = output_root / "pr20d_real_exec_rounds.csv"
    queries_path = output_root / "pr20d_real_exec_queries.csv"
    _write_csv(summary_path, SUMMARY_COLUMNS, [summary])
    _write_csv(rounds_path, ROUND_COLUMNS, round_rows)
    _write_csv(queries_path, QUERY_COLUMNS, query_rows)
    return summary_path, rounds_path, queries_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PR20d offline real-exec prefix-swap validation.")
    p.add_argument("--benchmark", default="job")
    p.add_argument("--workload-type", default="random")
    p.add_argument("--round-size", type=int, default=33)
    p.add_argument("--database", required=True, help="Explicit scratch/local benchmark database name.")
    p.add_argument("--metrics-csv", type=Path, required=True)
    p.add_argument("--pr20c-rounds-csv", type=Path, required=True)
    p.add_argument("--pr20c-candidates-csv", type=Path, required=True)
    p.add_argument("--output-root", type=Path, default=Path("runs_pr20d_real_exec_prefix_swap"))
    p.add_argument("--prefix-index", default=DEFAULT_PREFIX_INDEX)
    p.add_argument("--composite-index", default=DEFAULT_COMPOSITE_INDEX)
    p.add_argument("--max-num", type=int, default=10)
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--control-count", type=int, default=1)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--run-label", default="job_prefix_swap")
    p.add_argument("--experimental-physical-indexes", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    ensure_experimental_allowed(
        experimental_physical_indexes=bool(args.experimental_physical_indexes),
        database=str(args.database),
    )

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
            top_k=int(args.top_k),
            control_count=int(args.control_count),
            warmup=int(args.warmup),
            repeats=int(args.repeats),
            threshold=float(args.threshold),
            run_label=str(args.run_label),
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
