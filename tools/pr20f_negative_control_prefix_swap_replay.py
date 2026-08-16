#!/usr/bin/env python3
"""PR20f offline negative-control replay for the JOB movie_info prefix swap.

This tool reuses the PR20d/PR20e physical replay path but changes the round
selection and output reporting to test offline accept/reject gate behavior. It
does not change AdaSelectPP online policy, candidate generation, scoring,
selection, evaluation-budget, optimizer-ratio, or materialization behavior.
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
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

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
from tools.pr20e_broader_prefix_swap_replay import (
    coeff_var,
    make_run_order,
    mean,
    median,
    outcome_for_rel,
    query_concentration,
    stdev,
    unstable_reason_for,
)


OUTPUT_ROOT = Path("runs_pr20f_negative_control_prefix_swap_replay")
WHATIF_WIN_THRESHOLD = 0.005
OUTCOME_THRESHOLD = 0.01
DEFAULT_MAX_CV = 0.20
DEFAULT_GATE_REL_THRESHOLDS = (0.01, 0.02, 0.03, 0.05)
DEFAULT_GATE_MARGIN_THRESHOLD = 0.03
DEFAULT_NEAR_MARGIN_BAND = 0.005

ROUND_COLUMNS = [
    "round_id",
    "sample_category",
    "unstable_excluded",
    "unstable_reason",
    "old_config",
    "swap_config",
    "prefix_index",
    "composite_index",
    "target_swap_whatif_rel_improvement",
    "best_swap_index",
    "best_swap_whatif_rel_improvement",
    "is_target_best",
    "gate_threshold",
    "gate_accept",
    "gate_reject",
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
    "real_outcome",
    "gate_outcome",
    "plan_uses_prefix_count",
    "plan_uses_composite_count",
    "query_level_concentration",
    "num_queries",
    "prefix_index_size_bytes",
    "composite_index_size_bytes",
    "storage_delta_bytes",
    "storage_delta_ratio",
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

EXCLUDED_COLUMNS = [
    "round_id",
    "original_sample_category",
    "unstable_reason",
    "baseline_cv",
    "swap_cv",
    "baseline_exec_ms_all",
    "swap_exec_ms_all",
    "target_swap_whatif_rel_improvement",
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
    "notes",
]

GATE_METRICS_COLUMNS = [
    "threshold",
    "tested_count",
    "accept_count",
    "reject_count",
    "true_accept_count",
    "false_accept_count",
    "true_reject_count",
    "false_reject_count",
    "accept_precision",
    "reject_success_rate",
    "false_accept_rate",
    "false_reject_rate",
]


@dataclass(frozen=True)
class TargetCandidateInfo:
    round_id: int
    target_swap_whatif_rel_improvement: float
    best_swap_index: str
    best_swap_whatif_rel_improvement: float
    is_target_best: bool
    oracle_pass_swap: bool


@dataclass(frozen=True)
class SelectedRound:
    round_id: int
    sample_category: str
    target_swap_whatif_rel_improvement: float
    best_swap_index: str
    best_swap_whatif_rel_improvement: float
    is_target_best: bool
    notes: str = ""


@dataclass
class WorkloadRun:
    total_ms: float
    per_query_ms: List[float]
    plan_uses_index: List[bool]


class PR20fIndexCollisionError(RuntimeError):
    """Raised before a round mutates a pre-existing exact target name."""


class PR20fStrictCleanupError(RuntimeError):
    """Raised when final run-owned physical-index cleanup is not proven complete."""


def _fmt(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.12g}"
    return value


def _json_list(values: Sequence[object]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _json_float_list(values: Sequence[float]) -> str:
    return json.dumps([round(float(v), 6) for v in values], separators=(",", ":"))


def _row_float(row: Mapping[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(str(row.get(key, "")).strip())
    except Exception:
        return default


def _row_true(row: Mapping[str, str], key: str) -> bool:
    return str(row.get(key, "")).strip().lower() in {"1", "1.0", "true", "yes"}


def _safe_div(numer: float, denom: float) -> float:
    return float(numer) / float(denom) if abs(float(denom)) > 1e-12 else 0.0


def _sql_literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def parse_gate_thresholds(text: str) -> List[float]:
    values: List[float] = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        value = float(item)
        if value < 0.0:
            raise ValueError(f"gate threshold must be non-negative: {value}")
        values.append(value)
    if not values:
        raise ValueError("at least one gate threshold is required")
    return sorted(set(values))


def read_target_candidate_infos(
    *,
    rounds_csv: Path,
    candidates_csv: Path,
    composite_index: IndexKey,
) -> Dict[int, TargetCandidateInfo]:
    composite_name = format_candidate_key(composite_index)
    target_rows = read_pr20c_target_rows(candidates_csv, composite_index)
    round_rows = read_pr20c_round_rows(rounds_csv)
    infos: Dict[int, TargetCandidateInfo] = {}
    for rid, target_row in target_rows.items():
        round_row = round_rows.get(rid, {})
        best_index = str(round_row.get("best_swap_index", "")).strip()
        best_rel = _row_float(round_row, "best_swap_relative_improvement")
        target_rel = _row_float(target_row, "swap_relative_improvement")
        infos[int(rid)] = TargetCandidateInfo(
            round_id=int(rid),
            target_swap_whatif_rel_improvement=float(target_rel),
            best_swap_index=best_index,
            best_swap_whatif_rel_improvement=float(best_rel),
            is_target_best=best_index == composite_name,
            oracle_pass_swap=_row_true(target_row, "oracle_pass_swap"),
        )
    return infos


def is_positive_arm_census_round(info: TargetCandidateInfo, *, whatif_win_threshold: float) -> bool:
    return bool(
        info.is_target_best
        and info.oracle_pass_swap
        and info.target_swap_whatif_rel_improvement >= float(whatif_win_threshold)
    )


def categorize_negative_control_round(
    info: TargetCandidateInfo,
    *,
    gate_margin_threshold: float,
    near_margin_band: float,
    include_round22_reference: bool = True,
) -> str:
    rel = float(info.target_swap_whatif_rel_improvement)
    near_margin = rel > 0.0 and abs(rel - float(gate_margin_threshold)) <= float(near_margin_band)
    if include_round22_reference and info.round_id == 22 and rel > 0.0:
        return "near_margin"
    if near_margin:
        return "near_margin"
    if rel <= 0.0:
        return "predicted_negative"
    if not info.is_target_best:
        return "non_target_best_positive"
    return "predicted_flat_or_low"


def _category_sort_key(category: str, item: SelectedRound, *, gate_margin_threshold: float) -> Tuple[float, int]:
    rel = float(item.target_swap_whatif_rel_improvement)
    if category == "non_target_best_positive":
        return (-rel, item.round_id)
    if category == "near_margin":
        return (abs(rel - float(gate_margin_threshold)), item.round_id)
    if category == "predicted_negative":
        return (rel, item.round_id)
    if category == "positive_anchor_optional":
        return (-rel, item.round_id)
    return (abs(rel), item.round_id)


def select_negative_control_rounds(
    *,
    rounds_csv: Path,
    candidates_csv: Path,
    metrics_csv: Path,
    prefix_index: IndexKey,
    composite_index: IndexKey,
    max_num: int,
    selection_mode: str = "all",
    max_rounds_per_category: int = 0,
    positive_anchor_count: int = 0,
    whatif_win_threshold: float = WHATIF_WIN_THRESHOLD,
    gate_margin_threshold: float = DEFAULT_GATE_MARGIN_THRESHOLD,
    near_margin_band: float = DEFAULT_NEAR_MARGIN_BAND,
    include_round22_reference: bool = True,
) -> List[SelectedRound]:
    """Select feasible target-swap rounds for negative-control replay."""
    baselines = read_executed_configs(metrics_csv)
    infos = read_target_candidate_infos(
        rounds_csv=rounds_csv,
        candidates_csv=candidates_csv,
        composite_index=composite_index,
    )
    prefix = normalize_candidate_key(prefix_index)
    composite = normalize_candidate_key(composite_index)
    negative_groups: Dict[str, List[SelectedRound]] = {
        "non_target_best_positive": [],
        "predicted_flat_or_low": [],
        "predicted_negative": [],
        "near_margin": [],
    }
    positive_anchors: List[SelectedRound] = []

    for rid, info in sorted(infos.items()):
        baseline = baselines.get(rid)
        if baseline is None:
            continue
        swap_config, feasible, reason = build_prefix_swap_config(
            baseline,
            prefix,
            composite,
            max_num=max_num,
        )
        if not feasible or swap_config is None:
            continue

        row = SelectedRound(
            round_id=rid,
            sample_category="",
            target_swap_whatif_rel_improvement=info.target_swap_whatif_rel_improvement,
            best_swap_index=info.best_swap_index,
            best_swap_whatif_rel_improvement=info.best_swap_whatif_rel_improvement,
            is_target_best=info.is_target_best,
            notes="feasible_prefix_swap",
        )
        positive_arm = is_positive_arm_census_round(info, whatif_win_threshold=whatif_win_threshold)
        category = categorize_negative_control_round(
            info,
            gate_margin_threshold=gate_margin_threshold,
            near_margin_band=near_margin_band,
            include_round22_reference=include_round22_reference,
        )
        if positive_arm and category != "near_margin":
            if positive_anchor_count > 0:
                positive_anchors.append(SelectedRound(**{**row.__dict__, "sample_category": "positive_anchor_optional"}))
            continue
        negative_groups[category].append(SelectedRound(**{**row.__dict__, "sample_category": category}))

    selected: List[SelectedRound] = []
    use_stratified = str(selection_mode).strip().lower() == "stratified"
    for category, items in negative_groups.items():
        ordered = sorted(items, key=lambda item, cat=category: _category_sort_key(cat, item, gate_margin_threshold=gate_margin_threshold))
        if use_stratified and int(max_rounds_per_category) > 0:
            ordered = ordered[: int(max_rounds_per_category)]
        selected.extend(ordered)

    anchors = sorted(
        positive_anchors,
        key=lambda item: _category_sort_key("positive_anchor_optional", item, gate_margin_threshold=gate_margin_threshold),
    )
    selected.extend(anchors[: max(0, int(positive_anchor_count))])
    return sorted(selected, key=lambda item: (item.round_id, item.sample_category))


def pr20f_index_name(*, run_label: str, round_id: int, config_label: str, index: IndexKey) -> str:
    idx_text = format_candidate_key(index)
    digest = hashlib.md5(f"pr20f|{run_label}|{round_id}|{config_label}|{idx_text}".encode("utf-8")).hexdigest()[:10]
    table, cols = normalize_candidate_key(index)
    body = re.sub(r"[^a-zA-Z0-9_]+", "_", f"{run_label}_{round_id}_{config_label}_{table}_{'_'.join(cols)}".lower()).strip("_")
    prefix = "pr20f_"
    max_body = 63 - len(prefix) - len(digest) - 1
    return f"{prefix}{body[:max_body]}_{digest}"


def generate_pr20f_index_ddls(
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
        name = pr20f_index_name(
            run_label=run_label,
            round_id=int(round_id),
            config_label=config_label,
            index=idx,
        )
        create_sql = f"CREATE INDEX {quote_ident(name)} ON {quote_ident(table)} ({', '.join(quote_ident(c) for c in cols)})"
        drop_sql = f"DROP INDEX IF EXISTS {quote_ident(name)}"
        ddls.append(IndexDDL(index=idx, name=name, create_sql=create_sql, drop_sql=drop_sql))
    return ddls


def _deduplicate_index_ddls(ddls: Iterable[IndexDDL]) -> Dict[str, IndexDDL]:
    by_name: Dict[str, IndexDDL] = {}
    for ddl in ddls:
        existing = by_name.get(ddl.name)
        if existing is not None and existing != ddl:
            raise ValueError(f"conflicting PR20f DDL definitions for exact index name {ddl.name!r}")
        by_name[ddl.name] = ddl
    return by_name


def _existing_catalog_names(db, names: Iterable[str]) -> Set[str]:
    exact_names = sorted({str(name) for name in names if str(name)})
    if not exact_names:
        return set()
    rows = db.exec_fetchall_params(
        "SELECT c.relname::text FROM pg_catalog.pg_class AS c "
        "WHERE c.relname = ANY(%s) "
        "ORDER BY c.relname",
        (exact_names,),
    )
    return {str(row[0]) for row in rows if row and str(row[0]) in exact_names}


def register_run_owned_indexes(
    db,
    run_owned_indexes: MutableMapping[str, IndexDDL],
    round_ddls: Iterable[IndexDDL],
) -> None:
    """Register a round only after proving its new exact target names are absent."""

    unique = _deduplicate_index_ddls(round_ddls)
    for name, ddl in unique.items():
        registered = run_owned_indexes.get(name)
        if registered is not None and registered != ddl:
            raise ValueError(f"run-owned registry conflict for exact index name {name!r}")

    new_names = sorted(name for name in unique if name not in run_owned_indexes)
    try:
        conflicts = sorted(_existing_catalog_names(db, new_names))
    except Exception as exc:
        raise PR20fIndexCollisionError(
            "could not verify exact PR20f target-name absence before physical mutation"
        ) from exc
    if conflicts:
        raise PR20fIndexCollisionError(
            "pre-existing exact PR20f target index name(s): " + ", ".join(conflicts)
        )

    for name in new_names:
        run_owned_indexes[name] = unique[name]


def strict_cleanup_run_owned_indexes(
    db,
    run_owned_indexes: Mapping[str, IndexDDL],
) -> None:
    """Drop and verify only exact names registered as owned by this PR20f run."""

    owned = _deduplicate_index_ddls(run_owned_indexes.values())
    failures: List[str] = []
    for ddl in reversed(list(owned.values())):
        try:
            db.execute_only(ddl.drop_sql)
        except Exception as exc:
            rollback_error = None
            try:
                db.rollback()
            except Exception as rollback_exc:
                rollback_error = rollback_exc
            detail = f"{ddl.name}: {type(exc).__name__}: {exc}"
            if rollback_error is not None:
                detail += f"; rollback {type(rollback_error).__name__}: {rollback_error}"
            failures.append(detail)

    try:
        remaining = sorted(_existing_catalog_names(db, owned))
    except Exception as exc:
        failures.append(f"verification: {type(exc).__name__}: {exc}")
        remaining = []
    if remaining:
        failures.append("remaining exact index name(s): " + ", ".join(remaining))
    if failures:
        raise PR20fStrictCleanupError(
            "PR20f strict physical-index cleanup failed: " + " | ".join(failures)
        )


def _pick_index_name(ddls: Sequence[IndexDDL], index: IndexKey) -> str:
    target = normalize_candidate_key(index)
    return next((ddl.name for ddl in ddls if normalize_candidate_key(ddl.index) == target), "")


def _index_size_bytes(db, index_name: str) -> object:
    if not index_name:
        return ""
    try:
        regclass = quote_ident(index_name)
        row = db.execute_and_fetch(f"SELECT pg_relation_size({_sql_literal(regclass)}::regclass)", True)
        if row and row[0]:
            return int(row[0][0])
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    return ""


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


def gate_outcome_for(*, gate_accept: bool, real_outcome: str, unstable: bool = False) -> str:
    if unstable or real_outcome == "excluded_unstable":
        return "excluded_unstable"
    if gate_accept and real_outcome == "improved":
        return "true_accept"
    if gate_accept and real_outcome in {"flat", "worse"}:
        return "false_accept"
    if (not gate_accept) and real_outcome in {"flat", "worse"}:
        return "true_reject"
    if (not gate_accept) and real_outcome == "improved":
        return "false_reject"
    raise ValueError(f"unsupported real_outcome: {real_outcome}")


def expand_gate_rows(
    base_row: Mapping[str, object],
    *,
    gate_thresholds: Sequence[float],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    target_rel = float(base_row.get("target_swap_whatif_rel_improvement", 0.0) or 0.0)
    unstable = bool(int(base_row.get("unstable_excluded", 0) or 0))
    real_outcome = str(base_row.get("real_outcome", "flat"))
    for threshold in gate_thresholds:
        gate_accept = target_rel >= float(threshold)
        row = dict(base_row)
        row.update({
            "gate_threshold": float(threshold),
            "gate_accept": int(gate_accept),
            "gate_reject": int(not gate_accept),
            "gate_outcome": gate_outcome_for(
                gate_accept=gate_accept,
                real_outcome=real_outcome,
                unstable=unstable,
            ),
        })
        rows.append(row)
    return rows


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
    gate_thresholds: Sequence[float],
    run_label: str,
    run_order_id: str,
    run_owned_indexes: MutableMapping[str, IndexDDL],
) -> Tuple[List[Mapping[str, object]], List[Mapping[str, object]], Optional[Mapping[str, object]]]:
    if int(warmup) < 1:
        raise ValueError("PR20f requires warmup >= 1")
    if int(repeats) < 3:
        raise ValueError("PR20f requires repeats >= 3")

    order = make_run_order(repeats, run_order_id)
    swap_config, feasible, reason = build_prefix_swap_config(
        baseline_config,
        prefix_index,
        composite_index,
        max_num=max_num,
    )
    if not feasible or swap_config is None:
        base_row = {
            "round_id": selected_round.round_id,
            "sample_category": "excluded_unstable",
            "unstable_excluded": 1,
            "unstable_reason": reason,
            "old_config": config_to_string(baseline_config),
            "swap_config": "",
            "prefix_index": format_candidate_key(prefix_index),
            "composite_index": format_candidate_key(composite_index),
            "target_swap_whatif_rel_improvement": selected_round.target_swap_whatif_rel_improvement,
            "best_swap_index": selected_round.best_swap_index,
            "best_swap_whatif_rel_improvement": selected_round.best_swap_whatif_rel_improvement,
            "is_target_best": int(selected_round.is_target_best),
            "baseline_exec_ms_all": "[]",
            "swap_exec_ms_all": "[]",
            "real_outcome": "excluded_unstable",
            "notes": f"original_category={selected_round.sample_category};{reason};{selected_round.notes}",
        }
        excluded = {
            "round_id": selected_round.round_id,
            "original_sample_category": selected_round.sample_category,
            "unstable_reason": reason,
            "baseline_cv": "",
            "swap_cv": "",
            "baseline_exec_ms_all": "[]",
            "swap_exec_ms_all": "[]",
            "target_swap_whatif_rel_improvement": selected_round.target_swap_whatif_rel_improvement,
            "notes": base_row["notes"],
        }
        return expand_gate_rows(base_row, gate_thresholds=gate_thresholds), [], excluded

    baseline_ddls = generate_pr20f_index_ddls(
        baseline_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="baseline",
        max_num=max_num,
    )
    swap_ddls = generate_pr20f_index_ddls(
        swap_config,
        run_label=run_label,
        round_id=selected_round.round_id,
        config_label="swap",
        max_num=max_num,
    )
    all_ddls = list(baseline_ddls) + list(swap_ddls)
    register_run_owned_indexes(db, run_owned_indexes, all_ddls)
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
    prefix_size: object = ""
    composite_size: object = ""

    try:
        for label in order:
            ddls = ddls_by_label[label]
            try:
                drop_config_indexes(db, all_ddls)
                materialize_config(db, ddls)
                if label == "baseline" and prefix_size == "":
                    prefix_size = _index_size_bytes(db, prefix_name)
                if label == "swap" and composite_size == "":
                    composite_size = _index_size_bytes(db, composite_name)
                if not warmed[label]:
                    for _ in range(int(warmup)):
                        _execute_workload_once(db, workload, watched_by_label[label])
                    warmed[label] = True
                result = _execute_workload_once(db, workload, watched_by_label[label])
            finally:
                drop_config_indexes(db, all_ddls)
            totals[label].append(result.total_ms)
            for qid, runtime in enumerate(result.per_query_ms):
                per_query[label][qid].append(runtime)
            for qid, used in enumerate(result.plan_uses_index):
                plan_uses[label][qid] = bool(plan_uses[label][qid] or used)
    finally:
        drop_config_indexes(db, all_ddls)

    baseline_median = median(totals["baseline"])
    swap_median = median(totals["swap"])
    base_cv = coeff_var(totals["baseline"])
    swap_cv = coeff_var(totals["swap"])
    unstable_reason = unstable_reason_for(base_cv, swap_cv, max_cv)
    unstable = bool(unstable_reason)
    real_delta = baseline_median - swap_median
    real_rel = relative_improvement(baseline_median, swap_median)
    real_outcome = outcome_for_rel(real_rel, threshold=outcome_threshold, unstable=unstable)

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

    storage_delta: object = ""
    storage_ratio: object = ""
    if isinstance(prefix_size, int) and isinstance(composite_size, int):
        storage_delta = int(composite_size) - int(prefix_size)
        storage_ratio = _safe_div(float(storage_delta), float(prefix_size))

    notes = f"original_category={selected_round.sample_category};warmup={int(warmup)};repeats={int(repeats)};{selected_round.notes}"
    base_row = {
        "round_id": selected_round.round_id,
        "sample_category": "excluded_unstable" if unstable else selected_round.sample_category,
        "unstable_excluded": int(unstable),
        "unstable_reason": unstable_reason,
        "old_config": config_to_string(baseline_config),
        "swap_config": config_to_string(swap_config),
        "prefix_index": format_candidate_key(prefix_index),
        "composite_index": format_candidate_key(composite_index),
        "target_swap_whatif_rel_improvement": selected_round.target_swap_whatif_rel_improvement,
        "best_swap_index": selected_round.best_swap_index,
        "best_swap_whatif_rel_improvement": selected_round.best_swap_whatif_rel_improvement,
        "is_target_best": int(selected_round.is_target_best),
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
        "real_outcome": real_outcome,
        "plan_uses_prefix_count": sum(1 for x in plan_uses["baseline"] if x),
        "plan_uses_composite_count": sum(1 for x in plan_uses["swap"] if x),
        "query_level_concentration": query_concentration(query_deltas),
        "num_queries": len(workload),
        "prefix_index_size_bytes": prefix_size,
        "composite_index_size_bytes": composite_size,
        "storage_delta_bytes": storage_delta,
        "storage_delta_ratio": storage_ratio,
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
            "target_swap_whatif_rel_improvement": selected_round.target_swap_whatif_rel_improvement,
            "notes": notes,
        }
    return expand_gate_rows(base_row, gate_thresholds=gate_thresholds), query_rows, excluded


def _safe_stats(values: Sequence[float]) -> Tuple[float, float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0, 0.0
    return mean(values), median(values), min(values), max(values)


def summarize_category_metrics(
    round_rows: Sequence[Mapping[str, object]],
    excluded_rows: Sequence[Mapping[str, object]],
) -> List[Mapping[str, object]]:
    primary_by_round: Dict[int, Mapping[str, object]] = {}
    for row in round_rows:
        if int(row.get("unstable_excluded", 0) or 0):
            continue
        rid = int(row.get("round_id", 0) or 0)
        primary_by_round.setdefault(rid, row)
    primary = list(primary_by_round.values())

    rows: List[Mapping[str, object]] = []
    categories = [
        "non_target_best_positive",
        "predicted_flat_or_low",
        "predicted_negative",
        "near_margin",
        "positive_anchor_optional",
    ]
    for category in categories:
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
            "improved_count": sum(1 for row in cat_rows if row.get("real_outcome") == "improved"),
            "worse_count": sum(1 for row in cat_rows if row.get("real_outcome") == "worse"),
            "flat_count": sum(1 for row in cat_rows if row.get("real_outcome") == "flat"),
        })

    rows.append({
        "row_type": "excluded_unstable",
        "sample_category": "excluded_unstable",
        "excluded_round_count": len(excluded_rows),
        "excluded_round_ids": ";".join(str(row.get("round_id")) for row in excluded_rows),
        "notes": "excluded rows are omitted from category aggregates",
    })
    return rows


def summarize_gate_metrics(round_rows: Sequence[Mapping[str, object]]) -> List[Mapping[str, object]]:
    by_threshold: Dict[float, List[Mapping[str, object]]] = {}
    for row in round_rows:
        if int(row.get("unstable_excluded", 0) or 0):
            continue
        threshold = float(row.get("gate_threshold", 0.0) or 0.0)
        by_threshold.setdefault(threshold, []).append(row)

    rows: List[Mapping[str, object]] = []
    for threshold in sorted(by_threshold):
        rows_for_threshold = by_threshold[threshold]
        accept = [row for row in rows_for_threshold if int(row.get("gate_accept", 0) or 0)]
        reject = [row for row in rows_for_threshold if int(row.get("gate_reject", 0) or 0)]
        true_accept = [row for row in rows_for_threshold if row.get("gate_outcome") == "true_accept"]
        false_accept = [row for row in rows_for_threshold if row.get("gate_outcome") == "false_accept"]
        true_reject = [row for row in rows_for_threshold if row.get("gate_outcome") == "true_reject"]
        false_reject = [row for row in rows_for_threshold if row.get("gate_outcome") == "false_reject"]
        rows.append({
            "threshold": threshold,
            "tested_count": len(rows_for_threshold),
            "accept_count": len(accept),
            "reject_count": len(reject),
            "true_accept_count": len(true_accept),
            "false_accept_count": len(false_accept),
            "true_reject_count": len(true_reject),
            "false_reject_count": len(false_reject),
            "accept_precision": len(true_accept) / max(len(accept), 1),
            "reject_success_rate": len(true_reject) / max(len(reject), 1),
            "false_accept_rate": len(false_accept) / max(len(accept), 1),
            "false_reject_rate": len(false_reject) / max(len(reject), 1),
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
    max_rounds_per_category: int,
    positive_anchor_count: int,
    gate_thresholds: Sequence[float],
    gate_margin_threshold: float,
    near_margin_band: float,
    warmup: int,
    repeats: int,
    max_cv: float,
    outcome_threshold: float,
    run_label: str,
    run_order_id: str,
    run_owned_indexes: MutableMapping[str, IndexDDL],
) -> Tuple[Path, Path, Path, Path, Path]:
    baselines = read_executed_configs(metrics_csv)
    selected = select_negative_control_rounds(
        rounds_csv=pr20c_rounds_csv,
        candidates_csv=pr20c_candidates_csv,
        metrics_csv=metrics_csv,
        prefix_index=prefix_index,
        composite_index=composite_index,
        max_num=max_num,
        selection_mode=selection_mode,
        max_rounds_per_category=max_rounds_per_category,
        positive_anchor_count=positive_anchor_count,
        gate_margin_threshold=gate_margin_threshold,
        near_margin_band=near_margin_band,
    )
    if not selected:
        raise RuntimeError("no feasible PR20f negative-control target rounds selected")

    round_rows: List[Mapping[str, object]] = []
    query_rows: List[Mapping[str, object]] = []
    excluded_rows: List[Mapping[str, object]] = []
    for item in selected:
        if item.round_id < 0 or item.round_id >= len(workloads):
            raise IndexError(f"round_id={item.round_id} missing from loaded workloads")
        baseline = baselines.get(item.round_id)
        if baseline is None:
            raise KeyError(f"round_id={item.round_id} missing executed baseline config")
        rows, qrows, excluded = evaluate_round(
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
            gate_thresholds=gate_thresholds,
            run_label=run_label,
            run_order_id=run_order_id,
            run_owned_indexes=run_owned_indexes,
        )
        round_rows.extend(rows)
        query_rows.extend(qrows)
        if excluded is not None:
            excluded_rows.append(excluded)

    output_root = Path(output_root)
    summary_path = output_root / "pr20f_negative_control_summary.csv"
    rounds_path = output_root / "pr20f_negative_control_rounds.csv"
    queries_path = output_root / "pr20f_negative_control_queries.csv"
    excluded_path = output_root / "pr20f_negative_control_excluded_unstable.csv"
    gate_metrics_path = output_root / "pr20f_negative_control_gate_metrics.csv"
    write_csv(summary_path, SUMMARY_COLUMNS, summarize_category_metrics(round_rows, excluded_rows))
    write_csv(rounds_path, ROUND_COLUMNS, round_rows)
    write_csv(queries_path, QUERY_COLUMNS, query_rows)
    write_csv(excluded_path, EXCLUDED_COLUMNS, excluded_rows)
    write_csv(gate_metrics_path, GATE_METRICS_COLUMNS, summarize_gate_metrics(round_rows))
    return summary_path, rounds_path, queries_path, excluded_path, gate_metrics_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PR20f negative-control prefix-swap replay diagnostic.")
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
    p.add_argument("--max-rounds-per-category", type=int, default=0)
    p.add_argument("--positive-anchor-count", type=int, default=0)
    p.add_argument("--gate-rel-thresholds", default=",".join(str(x) for x in DEFAULT_GATE_REL_THRESHOLDS))
    p.add_argument("--gate-margin-threshold", type=float, default=DEFAULT_GATE_MARGIN_THRESHOLD)
    p.add_argument("--near-margin-band", type=float, default=DEFAULT_NEAR_MARGIN_BAND)
    p.add_argument("--warmup", type=int, default=1)
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--max-cv", type=float, default=DEFAULT_MAX_CV)
    p.add_argument("--outcome-threshold", type=float, default=OUTCOME_THRESHOLD)
    p.add_argument("--run-label", default="job_pr20f_negative_control")
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
        raise ValueError("PR20f requires --warmup >= 1")
    if int(args.repeats) < 3:
        raise ValueError("PR20f requires --repeats >= 3")

    from database.database_connector import DatabaseConnector

    workloads = load_workloads(args.benchmark, args.workload_type, int(args.round_size))
    db = DatabaseConnector(str(args.database), virtual=False, run_num=1)
    run_owned_indexes: Dict[str, IndexDDL] = {}
    paths: Optional[Tuple[Path, Path, Path, Path, Path]] = None
    experiment_error: Optional[BaseException] = None
    experiment_traceback = None
    cleanup_error: Optional[BaseException] = None
    cleanup_traceback = None
    close_error: Optional[BaseException] = None
    close_traceback = None
    try:
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
                max_rounds_per_category=int(args.max_rounds_per_category),
                positive_anchor_count=int(args.positive_anchor_count),
                gate_thresholds=parse_gate_thresholds(str(args.gate_rel_thresholds)),
                gate_margin_threshold=float(args.gate_margin_threshold),
                near_margin_band=float(args.near_margin_band),
                warmup=int(args.warmup),
                repeats=int(args.repeats),
                max_cv=float(args.max_cv),
                outcome_threshold=float(args.outcome_threshold),
                run_label=str(args.run_label),
                run_order_id=str(args.run_order_id),
                run_owned_indexes=run_owned_indexes,
            )
        except BaseException as exc:
            experiment_error = exc
            experiment_traceback = exc.__traceback__
    finally:
        try:
            strict_cleanup_run_owned_indexes(db, run_owned_indexes)
        except BaseException as exc:
            cleanup_error = exc
            cleanup_traceback = exc.__traceback__
        try:
            db.close()
        except BaseException as exc:
            close_error = exc
            close_traceback = exc.__traceback__

    finalization_errors = []
    if cleanup_error is not None:
        finalization_errors.append(
            f"cleanup={type(cleanup_error).__name__}: {cleanup_error}"
        )
    if close_error is not None:
        finalization_errors.append(f"close={type(close_error).__name__}: {close_error}")
    if experiment_error is not None:
        if finalization_errors:
            raise RuntimeError(
                f"PR20f experiment failed with {type(experiment_error).__name__}: "
                f"{experiment_error}; finalization also failed: "
                + "; ".join(finalization_errors)
            ) from experiment_error
        raise experiment_error.with_traceback(experiment_traceback)
    if cleanup_error is not None and close_error is not None:
        raise RuntimeError(
            "PR20f finalization failed: " + "; ".join(finalization_errors)
        ) from cleanup_error
    if cleanup_error is not None:
        raise cleanup_error.with_traceback(cleanup_traceback)
    if close_error is not None:
        raise close_error.with_traceback(close_traceback)
    if paths is None:
        raise RuntimeError("PR20f experiment produced no output paths")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
