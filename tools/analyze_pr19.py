#!/usr/bin/env python3
"""Analyze PR19 offline candidate-pool validation outputs."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.pr19_candidate_pool_common import MODES


Key = Tuple[str, str, int]


ROUND_COLUMNS = [
    "bench",
    "workload_type",
    "round_id",
    "pool_size_probe_grow",
    "pool_size_probe_grow_fair",
    "pool_size_delta",
    "width2_count_probe_grow",
    "width2_count_probe_grow_fair",
    "width2_count_delta",
    "selected_width2_probe_grow",
    "selected_width2_probe_grow_fair",
    "selected_width2_delta",
    "relative_improvement_probe_grow",
    "relative_improvement_probe_grow_fair",
    "improvement_delta",
    "selected_overlap_jaccard",
    "fair_win",
    "generation_time_probe_grow_ms",
    "generation_time_probe_grow_fair_ms",
    "generation_time_delta",
    "selector_time_probe_grow_ms",
    "selector_time_probe_grow_fair_ms",
    "selector_time_delta",
]

SUMMARY_COLUMNS = [
    "bench",
    "workload_type",
    "rounds",
    "fair_win_count",
    "fair_win_rate",
    "pool_size_delta_mean",
    "width2_count_delta_mean",
    "selected_width2_delta_mean",
    "improvement_delta_mean",
    "selected_overlap_jaccard_mean",
    "generation_time_delta_mean",
    "selector_time_delta_mean",
]

SELECTOR_METADATA_COLUMNS = [
    "selector_name",
    "selector_semantics",
    "liteselect_twocelf_imported",
]


def selected_index_set(value: str) -> Set[str]:
    return {part.strip() for part in str(value or "").split(";") if part.strip()}


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a = set(left)
    b = set(right)
    if not a and not b:
        return 1.0
    union = a | b
    return float(len(a & b)) / float(len(union)) if union else 1.0


def _float(row: Mapping[str, str], key: str) -> float:
    try:
        return float(row.get(key, 0.0) or 0.0)
    except Exception:
        return 0.0


def _int(row: Mapping[str, str], key: str) -> int:
    try:
        return int(float(row.get(key, 0) or 0))
    except Exception:
        return 0


def _read_validation_csv(path: Path) -> Dict[Key, Dict[str, str]]:
    out: Dict[Key, Dict[str, str]] = {}
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = (str(row.get("bench", "")), str(row.get("workload_type", "")), _int(row, "round_id"))
            out[key] = dict(row)
    return out


def _read_generation_times(path: Path) -> Dict[Key, float]:
    out: Dict[Key, float] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            key = (str(row.get("bench", "")), str(row.get("workload_type", "")), int(row.get("round_id", 0)))
            out[key] = float(row.get("generation_time_ms", 0.0) or 0.0)
    return out


def compute_round_deltas(
    probe_rows: Mapping[Key, Mapping[str, str]],
    fair_rows: Mapping[Key, Mapping[str, str]],
    probe_generation_times: Optional[Mapping[Key, float]] = None,
    fair_generation_times: Optional[Mapping[Key, float]] = None,
) -> List[Dict[str, object]]:
    probe_generation_times = probe_generation_times or {}
    fair_generation_times = fair_generation_times or {}
    deltas: List[Dict[str, object]] = []
    for key in sorted(set(probe_rows) & set(fair_rows)):
        bench, workload_type, round_id = key
        base = probe_rows[key]
        fair = fair_rows[key]
        for meta_col in SELECTOR_METADATA_COLUMNS:
            if str(base.get(meta_col, "")) != str(fair.get(meta_col, "")):
                raise ValueError(
                    "selector metadata mismatch for "
                    f"bench={bench} workload_type={workload_type} round_id={round_id} "
                    f"column={meta_col}: probe_grow={base.get(meta_col, '')!r} "
                    f"probe_grow_fair={fair.get(meta_col, '')!r}"
                )
        pool_delta = _int(fair, "pool_size") - _int(base, "pool_size")
        width2_delta = _int(fair, "width2_count") - _int(base, "width2_count")
        selected_width2_delta = _int(fair, "selected_width2") - _int(base, "selected_width2")
        improvement_delta = _float(fair, "relative_improvement") - _float(base, "relative_improvement")
        gen_probe = float(probe_generation_times.get(key, 0.0) or 0.0)
        gen_fair = float(fair_generation_times.get(key, 0.0) or 0.0)
        selector_probe = _float(base, "selector_time_ms")
        selector_fair = _float(fair, "selector_time_ms")
        overlap = jaccard(
            selected_index_set(str(base.get("selected_indexes", ""))),
            selected_index_set(str(fair.get("selected_indexes", ""))),
        )
        deltas.append(
            {
                "bench": bench,
                "workload_type": workload_type,
                "round_id": int(round_id),
                "pool_size_probe_grow": _int(base, "pool_size"),
                "pool_size_probe_grow_fair": _int(fair, "pool_size"),
                "pool_size_delta": int(pool_delta),
                "width2_count_probe_grow": _int(base, "width2_count"),
                "width2_count_probe_grow_fair": _int(fair, "width2_count"),
                "width2_count_delta": int(width2_delta),
                "selected_width2_probe_grow": _int(base, "selected_width2"),
                "selected_width2_probe_grow_fair": _int(fair, "selected_width2"),
                "selected_width2_delta": int(selected_width2_delta),
                "relative_improvement_probe_grow": _float(base, "relative_improvement"),
                "relative_improvement_probe_grow_fair": _float(fair, "relative_improvement"),
                "improvement_delta": float(improvement_delta),
                "selected_overlap_jaccard": float(overlap),
                "fair_win": int(improvement_delta > 0.0),
                "generation_time_probe_grow_ms": float(gen_probe),
                "generation_time_probe_grow_fair_ms": float(gen_fair),
                "generation_time_delta": float(gen_fair - gen_probe),
                "selector_time_probe_grow_ms": float(selector_probe),
                "selector_time_probe_grow_fair_ms": float(selector_fair),
                "selector_time_delta": float(selector_fair - selector_probe),
            }
        )
    return deltas


def summarize_round_deltas(rows: Sequence[Mapping[str, object]]) -> List[Dict[str, object]]:
    if not rows:
        return []

    def mean(key: str) -> float:
        return float(sum(float(row.get(key, 0.0) or 0.0) for row in rows)) / float(len(rows))

    bench = str(rows[0].get("bench", ""))
    workload_type = str(rows[0].get("workload_type", ""))
    fair_wins = sum(int(row.get("fair_win", 0) or 0) for row in rows)
    return [
        {
            "bench": bench,
            "workload_type": workload_type,
            "rounds": int(len(rows)),
            "fair_win_count": int(fair_wins),
            "fair_win_rate": float(fair_wins) / float(len(rows)),
            "pool_size_delta_mean": mean("pool_size_delta"),
            "width2_count_delta_mean": mean("width2_count_delta"),
            "selected_width2_delta_mean": mean("selected_width2_delta"),
            "improvement_delta_mean": mean("improvement_delta"),
            "selected_overlap_jaccard_mean": mean("selected_overlap_jaccard"),
            "generation_time_delta_mean": mean("generation_time_delta"),
            "selector_time_delta_mean": mean("selector_time_delta"),
        }
    ]


def _write_csv(path: Path, rows: Sequence[Mapping[str, object]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def analyze_pr19(output_base: Path) -> Tuple[Path, Path]:
    probe_dir = Path(output_base) / MODES[0]
    fair_dir = Path(output_base) / MODES[1]
    probe_rows = _read_validation_csv(probe_dir / "offline_validation.csv")
    fair_rows = _read_validation_csv(fair_dir / "offline_validation.csv")
    probe_gen = _read_generation_times(probe_dir / "candidate_pools.jsonl")
    fair_gen = _read_generation_times(fair_dir / "candidate_pools.jsonl")
    round_rows = compute_round_deltas(probe_rows, fair_rows, probe_gen, fair_gen)
    summary_rows = summarize_round_deltas(round_rows)
    round_path = Path(output_base) / "pr19_round_deltas.csv"
    summary_path = Path(output_base) / "pr19_summary.csv"
    _write_csv(round_path, round_rows, ROUND_COLUMNS)
    _write_csv(summary_path, summary_rows, SUMMARY_COLUMNS)
    return summary_path, round_path


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyze PR19 candidate-pool validation outputs.")
    p.add_argument("bench")
    p.add_argument("workload_type")
    p.add_argument("--output-root", type=Path, default=Path("runs_pr19_candidate_pool"))
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    output_base = Path(args.output_root) / f"{args.bench}_{args.workload_type}"
    summary_path, round_path = analyze_pr19(output_base)
    print(f"summary: {summary_path}")
    print(f"round_deltas: {round_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
