"""Shared PR19 candidate-pool helpers and offline selector metadata."""

from __future__ import annotations

from typing import Any, Iterable, List, Set, Tuple

from adaselect_pp.common import norm_name


IndexKey = Tuple[str, Tuple[str, ...]]
MODES = ("probe_grow", "probe_grow_fair")
SELECTOR_NAME = "offline_pool_celf"
SELECTOR_SEMANTICS = "pool_restricted_deterministic"
LITESELECT_TWOCELF_IMPORTED = "false"


def normalize_candidate_key(candidate: Any) -> IndexKey:
    """Return canonical ``(table, (cols...))`` from tuple/list/string input."""
    if isinstance(candidate, str):
        return parse_candidate_string(candidate)
    if not isinstance(candidate, (tuple, list)) or len(candidate) < 2:
        raise ValueError(f"invalid candidate key: {candidate!r}")
    table = norm_name(str(candidate[0]))
    raw_cols = candidate[1]
    if isinstance(raw_cols, str):
        cols_in = (raw_cols,)
    elif isinstance(raw_cols, (tuple, list)):
        cols_in = tuple(raw_cols)
    else:
        cols_in = tuple(candidate[1:])
    cols: List[str] = []
    seen: Set[str] = set()
    for col in cols_in:
        cc = norm_name(str(col))
        if cc and cc not in seen:
            seen.add(cc)
            cols.append(cc)
    if not table or not cols:
        raise ValueError(f"invalid candidate key: {candidate!r}")
    return table, tuple(cols)


def format_candidate_key(candidate: Any) -> str:
    table, cols = normalize_candidate_key(candidate)
    return f"{table}({','.join(cols)})"


def parse_candidate_string(text: str) -> IndexKey:
    raw = str(text or "").strip()
    if "(" not in raw or not raw.endswith(")"):
        raise ValueError(f"invalid candidate string: {text!r}")
    table, rest = raw.split("(", 1)
    cols_text = rest[:-1]
    cols = [norm_name(c) for c in cols_text.split(",") if norm_name(c)]
    if not norm_name(table) or not cols:
        raise ValueError(f"invalid candidate string: {text!r}")
    return normalize_candidate_key((table, tuple(cols)))


def normalized_candidate_strings(candidates: Iterable[Any]) -> List[str]:
    return sorted({format_candidate_key(candidate) for candidate in candidates})
