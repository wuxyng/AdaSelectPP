#!/usr/bin/env python3
"""Summarize Phase 0.5 first-pass run CSVs.

The script intentionally uses only the Python standard library so it can run
even on a partially prepared server environment after the experiment files
exist.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


NUMERIC_COLUMNS: Sequence[str] = (
    "candidate_count_raw",
    "candidate_count",
    "evaluated_count",
    "width1_count",
    "width2_count",
    "seed_count",
    "eligible_seed_count",
    "multi_growth_count",
    "structural_pair_quota",
    "structural_pair_eval_count",
    "structural_pair_eval_budgeted_out_count",
    "structural_pair_eval_lane_enabled",
)

TOTAL_COLUMNS: Sequence[str] = (
    "what_if_calls",
    "filtered_nonpositive_count",
    "rejected_growth_has_or",
    "rejected_growth_alias_ambiguous",
    "rejected_growth_seed_not_positive",
    "rejected_growth_seed_unseen",
    "rejected_growth_range_seed",
    "rejected_growth_parse_fallback",
)


def _as_float(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return float(text)
    except Exception:
        return default


def _as_int(value: object, default: int = 0) -> int:
    return int(_as_float(value, float(default)))


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return sum(vals) / float(len(vals))


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def _read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = []
        for row in reader:
            if str(row.get("round", "")).strip().upper() == "SUMMARY":
                continue
            rows.append(row)
        return rows


def _read_csv_rows(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _is_metrics_csv(path: Path) -> bool:
    if path.name.endswith(".trace.csv"):
        return False
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = set(reader.fieldnames or [])
        return {"round", "candidate_count", "evaluated_count"}.issubset(fields)
    except Exception:
        return False


def _is_trace_csv(path: Path) -> bool:
    if not path.name.endswith(".trace.csv"):
        return False
    try:
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            fields = set(reader.fieldnames or [])
        return {"round", "table", "cols", "in_appearing", "in_eval", "in_new"}.issubset(fields)
    except Exception:
        return False


def _under_preexisting_archive(path: Path, run_dir: Path) -> bool:
    try:
        rel = path.relative_to(run_dir)
    except ValueError:
        return False
    return "_preexisting_log_archive" in rel.parts


def _case_name(path: Path, run_dir: Path) -> str:
    try:
        rel = path.relative_to(run_dir)
    except ValueError:
        return path.stem
    if len(rel.parts) > 1:
        return rel.parts[0]
    return path.stem


def _trace_rows_for_case(csv_path: Path) -> Optional[List[Dict[str, str]]]:
    trace_paths = sorted(p for p in csv_path.parent.glob("*.trace.csv") if _is_trace_csv(p))
    if not trace_paths:
        return None
    rows: List[Dict[str, str]] = []
    for path in trace_paths:
        rows.extend(_read_csv_rows(path))
    return rows


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _width2_key(row: Dict[str, str]) -> str:
    table = str(row.get("table", "")).strip()
    cols = str(row.get("cols", "")).strip()
    return f"{table}({cols})" if table and cols else cols


def _is_width2_trace_row(row: Dict[str, str]) -> bool:
    cols = [c.strip() for c in str(row.get("cols", "")).split(",") if c.strip()]
    return len(cols) == 2


def _top_counter(counter: Counter, limit: int = 5) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{name}={count}" for name, count in counter.most_common(limit))


def _summarize_width2_trace(trace_rows: Optional[List[Dict[str, str]]]) -> List[str]:
    if trace_rows is None:
        return ["- width2_trace: unavailable"]

    width2_rows = [r for r in trace_rows if _is_width2_trace_row(r)]
    appeared = [r for r in width2_rows if _truthy(r.get("in_appearing"))]
    evaluated = [r for r in width2_rows if _truthy(r.get("in_eval"))]
    selected = [r for r in width2_rows if _truthy(r.get("in_new"))]
    zero_benefit = [
        r for r in width2_rows
        if str(r.get("benefit", "")).strip() != "" and abs(_as_float(r.get("benefit"))) <= 1e-12
    ]
    blocked_by_budget = [r for r in appeared if not _truthy(r.get("in_eval"))]

    by_appearance = Counter(_width2_key(r) for r in appeared)
    by_evaluation = Counter(_width2_key(r) for r in evaluated)
    by_selected = Counter(_width2_key(r) for r in selected)
    by_budget = Counter(_width2_key(r) for r in blocked_by_budget)
    replacement_rows = [r for r in width2_rows if str(r.get("replacement_benefit", "")).strip() != ""]
    by_replacement = Counter(
        _width2_key(r)
        for r in sorted(replacement_rows, key=lambda row: _as_float(row.get("replacement_benefit")), reverse=True)[:5]
    )
    per_round: Dict[str, Dict[str, str]] = {}
    for row in trace_rows:
        rid = str(row.get("round", "")).strip()
        if rid and rid not in per_round:
            per_round[rid] = row
    quota_total = sum(_as_int(r.get("structural_pair_quota")) for r in per_round.values())
    structural_eval_total = sum(_as_int(r.get("structural_pair_eval_count")) for r in per_round.values())
    budgeted_total = sum(_as_int(r.get("structural_pair_eval_budgeted_out_count")) for r in per_round.values())
    lane_rounds = sum(1 for r in per_round.values() if _truthy(r.get("structural_pair_eval_lane_enabled")))

    return [
        f"- width2_appeared_count: {len(appeared)}",
        f"- width2_evaluated_count: {len(evaluated)}",
        f"- width2_selected_count: {len(selected)}",
        f"- width2_with_zero_benefit_count: {len(zero_benefit)}",
        f"- structural_pair_quota_total: {quota_total}",
        f"- structural_pair_eval_count_total: {structural_eval_total}",
        f"- structural_pair_eval_budgeted_out_count_total: {budgeted_total}",
        f"- structural_pair_eval_lane_enabled_rounds: {lane_rounds}",
        f"- top_width2_by_appearance: {_top_counter(by_appearance)}",
        f"- top_width2_by_evaluation: {_top_counter(by_evaluation)}",
        f"- top_width2_by_selected_count: {_top_counter(by_selected)}",
        f"- top_width2_blocked_by_budget: {_top_counter(by_budget)}",
        f"- top_width2_by_replacement_diagnostic: {_top_counter(by_replacement)}",
    ]


def _summarize_case(name: str, csv_path: Path, rows: List[Dict[str, str]], trace_rows: Optional[List[Dict[str, str]]] = None) -> List[str]:
    warnings: List[str] = []
    total_rounds = len(rows)
    timeout_count = sum(_as_int(r.get("timeout")) for r in rows)
    switched_count = sum(_as_int(r.get("switched")) for r in rows if "switched" in r)
    gen_counts = Counter(str(r.get("gen_mode", "")).strip() or "(blank)" for r in rows)
    new_values = [str(r.get("new", "")).strip() for r in rows if str(r.get("new", "")).strip()]
    unique_new = len(set(new_values))

    for r in rows:
        rid = _as_int(r.get("round"), -1)
        width2 = _as_float(r.get("width2_count"))
        eligible_seed = _as_float(r.get("eligible_seed_count"))
        mode = str(r.get("gen_mode", "")).strip()
        if rid in (0, 1) and width2 > 0:
            warnings.append(f"round {rid} has width2_count={_fmt(width2)}")
        if rid in (0, 1) and mode and mode != "probe":
            warnings.append(f"round {rid} has gen_mode={mode!r}, expected 'probe'")
        if width2 > 0 and eligible_seed <= 0:
            warnings.append(f"round {rid} has width2_count={_fmt(width2)} while eligible_seed_count=0")

    parse_fallback_rejects = sum(_as_float(r.get("rejected_growth_parse_fallback")) for r in rows)
    if parse_fallback_rejects > max(10.0, total_rounds * 0.5):
        warnings.append(f"rejected_growth_parse_fallback is high: {_fmt(parse_fallback_rejects)}")
    if timeout_count > 0:
        warnings.append(f"timeout count > 0: {timeout_count}")

    raw_max = max((_as_float(r.get("candidate_count_raw")) for r in rows), default=0.0)
    cand_max = max((_as_float(r.get("candidate_count")) for r in rows), default=0.0)
    if raw_max > 500:
        warnings.append(f"candidate_count_raw max looks large: {_fmt(raw_max)}")
    if cand_max > 200:
        warnings.append(f"candidate_count max looks large: {_fmt(cand_max)}")

    lines = [
        f"### {name}",
        "",
        f"- csv: `{csv_path}`",
        f"- total_rounds: {total_rounds}",
        f"- timeout_count: {timeout_count}",
        f"- total_what_if_calls: {_fmt(sum(_as_float(r.get('what_if_calls')) for r in rows))}",
        f"- gen_mode_counts: {dict(gen_counts)}",
        f"- unique_new_conf_count: {unique_new}",
        f"- new_conf_count: {len(new_values)}",
        f"- switched_count: {switched_count}",
    ]

    for col in NUMERIC_COLUMNS:
        vals = [_as_float(r.get(col)) for r in rows]
        lines.append(f"- {col}: mean={_fmt(_mean(vals))} max={_fmt(max(vals, default=0.0))}")
    for col in TOTAL_COLUMNS:
        vals = [_as_float(r.get(col)) for r in rows]
        lines.append(f"- {col}_total: {_fmt(sum(vals))}")
    lines.extend(_summarize_width2_trace(trace_rows))

    if warnings:
        lines.append("- warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("- warnings: none")
    lines.append("")
    return lines


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/server/summarize_phase05.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[1]).resolve()
    if not run_dir.is_dir():
        print(f"Run directory not found: {run_dir}", file=sys.stderr)
        return 1

    csv_paths = sorted(
        p for p in run_dir.rglob("*.csv")
        if not _under_preexisting_archive(p, run_dir) and _is_metrics_csv(p)
    )
    out = run_dir / "summary.md"
    lines = [
        "# Phase 0.5 First-Pass Summary",
        "",
        f"- run_dir: `{run_dir}`",
        f"- metrics_csv_files: {len(csv_paths)}",
        "",
    ]
    if not csv_paths:
        lines.append("No metrics CSV files found.")
    for path in csv_paths:
        rows = _read_rows(path)
        lines.extend(_summarize_case(_case_name(path, run_dir), path, rows, _trace_rows_for_case(path)))
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
