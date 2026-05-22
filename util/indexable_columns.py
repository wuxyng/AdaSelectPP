# -*- coding: utf-8 -*-
"""Benchmark-specific indexable-column vocabulary loader.

These files are optional.  When present, they act as a whitelist for candidate
columns; they are NOT a candidate generator by themselves.  SQL/AST/role evidence
still decides which columns are active for a query.

Supported line formats are intentionally permissive:
  table.column
  table column
  table: col1 col2 col3
  table(col1,col2)
Blank lines and '#' comments are ignored.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable, Optional, Set


def _norm(s: str) -> str:
    return (s or "").strip().strip('"').lower()


def _candidate_paths(benchmark: Optional[str], explicit_path: Optional[str] = None) -> Iterable[Path]:
    if explicit_path:
        yield Path(explicit_path)
    if not benchmark:
        return
    name = str(benchmark).strip().lower()
    roots = [Path.cwd(), Path(__file__).resolve().parents[1], Path(__file__).resolve().parents[2]]
    rels = [
        Path("txt") / f"{name}_indexable_columns.txt",
        Path("database") / f"{name}_indexable_columns.txt",
        Path("database") / "workload" / f"{name}_indexable_columns.txt",
    ]
    seen = set()
    for root in roots:
        for rel in rels:
            p = (root / rel).resolve()
            if p in seen:
                continue
            seen.add(p)
            yield p


def _add(vocab: Dict[str, Set[str]], table: str, col: str) -> None:
    t = _norm(table)
    c = _norm(col)
    if not t or not c:
        return
    vocab.setdefault(t, set()).add(c)


def _parse_line(line: str, vocab: Dict[str, Set[str]]) -> None:
    raw = line.split("#", 1)[0].strip()
    if not raw:
        return

    # table: col1 col2 col3
    if ":" in raw and not raw.lower().startswith(("http:", "https:")):
        t, rest = raw.split(":", 1)
        for c in re.split(r"[\s,]+", rest.strip()):
            _add(vocab, t, c)
        return

    # table(col1,col2)
    m = re.match(r"^\s*([A-Za-z_]\w*)\s*\(([^)]*)\)\s*$", raw)
    if m:
        t = m.group(1)
        for c in re.split(r"[\s,]+", m.group(2).strip()):
            _add(vocab, t, c)
        return

    # table.column or schema.table.column (use last two components)
    toks = re.split(r"[\s,]+", raw)
    if len(toks) == 1 and "." in toks[0]:
        parts = [_norm(x) for x in toks[0].split(".") if _norm(x)]
        if len(parts) >= 2:
            _add(vocab, parts[-2], parts[-1])
        return

    # table column [possibly more columns]
    if len(toks) >= 2:
        t = toks[0]
        for c in toks[1:]:
            if "." in c:
                parts = [_norm(x) for x in c.split(".") if _norm(x)]
                if len(parts) >= 2:
                    _add(vocab, parts[-2], parts[-1])
                else:
                    _add(vocab, t, c)
            else:
                _add(vocab, t, c)


def load_indexable_columns(benchmark: Optional[str], explicit_path: Optional[str] = None) -> Dict[str, Set[str]]:
    """Load optional benchmark-specific indexable-column whitelist.

    Returns {} when no file is found.  Callers should treat an empty vocab as
    "no whitelist" rather than "no columns allowed".
    """
    for path in _candidate_paths(benchmark, explicit_path):
        try:
            if not path.exists():
                continue
            vocab: Dict[str, Set[str]] = {}
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    _parse_line(line, vocab)
            return vocab
        except Exception:
            continue
    return {}
