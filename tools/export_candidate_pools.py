#!/usr/bin/env python3
"""PR19 offline candidate-pool export harness.

This script compares candidate generation only. It does not call AdaSelect.run,
does not choose or materialize an online configuration, and does not change any
online policy code.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adasel.config_flags import parse_target_pair_audit
from adaselect_pp.candidate_gen_v2 import MCIGCandidateGenerator
from tools.pr19_candidate_pool_common import (
    MODES,
    IndexKey,
    format_candidate_key,
    normalize_candidate_key,
    normalized_candidate_strings,
    parse_candidate_string,
)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, tuple, list)):
        return [_json_safe(v) for v in value]
    return str(value)


def candidate_source_stats(stats: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        str(key): _json_safe(value)
        for key, value in sorted((stats or {}).items(), key=lambda kv: str(kv[0]))
        if not str(key).startswith("_")
    }


def candidate_pool_row(
    *,
    bench: str,
    workload_type: str,
    round_id: int,
    mode: str,
    result,
    generation_time_ms: float,
) -> Dict[str, Any]:
    candidates = {normalize_candidate_key(key) for key in (result.topk_set or set())}
    width1 = {key for key in candidates if len(key[1]) == 1}
    width2 = {key for key in candidates if len(key[1]) == 2}

    templates: Set[str] = set()
    for key in candidates:
        meta = (result.meta_map or {}).get(key, {}) or {}
        for tid in meta.get("template_ids", []) or []:
            if str(tid):
                templates.add(str(tid))

    return {
        "bench": str(bench),
        "workload_type": str(workload_type),
        "round_id": int(round_id),
        "mode": str(mode),
        "num_candidates": int(len(candidates)),
        "num_width1": int(len(width1)),
        "num_width2": int(len(width2)),
        "num_tables_covered": int(len({key[0] for key in candidates})),
        "num_templates_covered": int(len(templates)),
        "candidates": normalized_candidate_strings(candidates),
        "generation_time_ms": float(generation_time_ms),
        "candidate_source_stats": candidate_source_stats(result.stats or {}),
    }


def _clone_seed_state(seed_state: Mapping[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(dict(seed_state))


def _update_shared_width1_seed_state(seed_state: Dict[str, Any], rows: Sequence[Mapping[str, Any]], round_id: int) -> None:
    """Mode-independent offline seed state used only by the export harness."""
    seed_benefit = seed_state.setdefault("seed_benefit", {})
    seed_seen_count = seed_state.setdefault("seed_seen_count", {})
    seed_positive_count = seed_state.setdefault("seed_positive_count", {})
    seed_last_obs_src = seed_state.setdefault("seed_last_obs_src", {})
    seed_first_seen_round = seed_state.setdefault("seed_first_seen_round", {})
    seed_last_seen_round = seed_state.setdefault("seed_last_seen_round", {})
    seed_seen_rounds = seed_state.setdefault("seed_seen_rounds", {})
    seed_normalized_benefit = seed_state.setdefault("seed_normalized_benefit", {})

    for row in rows:
        for cand_text in row.get("candidates", []) or []:
            key = parse_candidate_string(cand_text)
            if len(key[1]) != 1:
                continue
            seed_benefit.setdefault(key, 1.0)
            seed_seen_count[key] = int(seed_seen_count.get(key, 0)) + 1
            seed_positive_count.setdefault(key, 1)
            seed_last_obs_src[key] = "PR19_SHARED_WIDTH1_SEED"
            seed_first_seen_round.setdefault(key, int(round_id))
            seed_last_seen_round[key] = int(round_id)
            seed_seen_rounds.setdefault(key, set()).add(int(round_id))
            seed_normalized_benefit.setdefault(key, 1.0)


def _make_generator(bench: str, db_con, cfg: Mapping[str, Any]) -> MCIGCandidateGenerator:
    max_num = int(cfg.get("max_num", 10))
    topk_factor = int(cfg.get("candidate_topk_factor", 4))
    topk_min_extra = int(cfg.get("candidate_topk_min_extra", 6))
    return MCIGCandidateGenerator(
        benchmark=bench,
        db_con=db_con,
        max_width=int(cfg.get("max_width", 2)),
        max_num=max(1, max_num * topk_factor + topk_min_extra),
        indexable_path=str(cfg.get("indexable_columns_path", "") or ""),
        per_query_cap=int(cfg.get("candidate_per_query_cap", 12)),
        per_table_cap=int(cfg.get("candidate_per_table_cap", 4)),
        round_table_cap=int(cfg.get("candidate_round_table_cap", 6)),
    )


def export_candidate_pools(
    *,
    bench: str,
    workload_type: str,
    workloads: Sequence[Sequence[str]],
    db_con,
    output_root: Path,
    cfg: Optional[Mapping[str, Any]] = None,
    modes: Sequence[str] = MODES,
    max_rounds: Optional[int] = None,
) -> Dict[str, Path]:
    cfg = dict(cfg or {})
    output_base = Path(output_root) / f"{bench}_{workload_type}"
    for mode in modes:
        mode_dir = output_base / mode
        mode_dir.mkdir(parents=True, exist_ok=True)
        pool_path = mode_dir / "candidate_pools.jsonl"
        pool_path.write_text("", encoding="utf-8")

    generators = {mode: _make_generator(bench, db_con, cfg) for mode in modes}
    topk = max(
        1,
        int(cfg.get("max_num", 10)) * int(cfg.get("candidate_topk_factor", 4))
        + int(cfg.get("candidate_topk_min_extra", 6)),
    )
    target_pair_audit = parse_target_pair_audit(cfg.get("target_pair_audit", set()) or set())
    seed_state: Dict[str, Any] = {
        "seed_benefit": {},
        "seed_seen_count": {},
        "seed_positive_count": {},
        "seed_last_obs_src": {},
        "seed_first_seen_round": {},
        "seed_last_seen_round": {},
        "seed_seen_rounds": {},
        "seed_normalized_benefit": {},
    }

    selected_workloads = list(workloads)
    if max_rounds is not None:
        selected_workloads = selected_workloads[: max(0, int(max_rounds))]

    for round_id, workload in enumerate(selected_workloads):
        round_seed = _clone_seed_state(seed_state)
        rows: List[Dict[str, Any]] = []
        for mode in modes:
            start = time.perf_counter()
            result = generators[mode].generate(
                list(workload),
                old_conf=set(),
                topk=topk,
                workload_count=int(round_id),
                candidate_generation_mode=mode,
                pair_supply_fairness_enabled=(mode == "probe_grow_fair"),
                pair_supply_per_table_width2_reserve=int(cfg.get("pair_supply_per_table_width2_reserve", 1)),
                pair_supply_round_width2_reserve=int(cfg.get("pair_supply_round_width2_reserve", 4)),
                target_pair_audit=target_pair_audit,
                **round_seed,
            )
            generation_time_ms = float((time.perf_counter() - start) * 1000.0)
            row = candidate_pool_row(
                bench=bench,
                workload_type=workload_type,
                round_id=round_id,
                mode=mode,
                result=result,
                generation_time_ms=generation_time_ms,
            )
            rows.append(row)
            pool_path = output_base / mode / "candidate_pools.jsonl"
            with pool_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        _update_shared_width1_seed_state(seed_state, rows, round_id)

    return {mode: output_base / mode / "candidate_pools.jsonl" for mode in modes}


def _load_cfg(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f) or {}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Export PR19 probe_grow/probe_grow_fair candidate pools.")
    p.add_argument("bench")
    p.add_argument("workload_type")
    p.add_argument("--round-size", type=int, default=50)
    p.add_argument("--max-rounds", type=int, default=None)
    p.add_argument("--output-root", type=Path, default=Path("runs_pr19_candidate_pool"))
    p.add_argument("--config", type=Path, default=Path("adasel/config/adaselect.json"))
    p.add_argument("--max-num", type=int, default=None)
    p.add_argument("--pair-supply-per-table-width2-reserve", type=int, default=None)
    p.add_argument("--pair-supply-round-width2-reserve", type=int, default=None)
    p.add_argument("--target-pair-audit", type=str, default="")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    from adasel.main import load_workloads
    from database.database_connector import DatabaseConnector

    cfg = _load_cfg(args.config)
    if args.max_num is not None:
        cfg["max_num"] = int(args.max_num)
    if args.pair_supply_per_table_width2_reserve is not None:
        cfg["pair_supply_per_table_width2_reserve"] = int(args.pair_supply_per_table_width2_reserve)
    if args.pair_supply_round_width2_reserve is not None:
        cfg["pair_supply_round_width2_reserve"] = int(args.pair_supply_round_width2_reserve)
    if args.target_pair_audit:
        cfg["target_pair_audit"] = parse_target_pair_audit(args.target_pair_audit)

    workloads = load_workloads(args.bench, args.workload_type, int(args.round_size))
    db = DatabaseConnector(args.bench, virtual=True, run_num=1)
    try:
        paths = export_candidate_pools(
            bench=args.bench,
            workload_type=args.workload_type,
            workloads=workloads,
            db_con=db,
            output_root=args.output_root,
            cfg=cfg,
            max_rounds=args.max_rounds,
        )
    finally:
        try:
            db.close()
        except Exception:
            pass
    for mode, path in paths.items():
        print(f"{mode}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
