# -*- coding: utf-8 -*-
"""BenefitNormalizer – normalises raw benefit & creation‑time signals.

Bug‑fix: load_creation_costs() now parses *_create_time.txt lines that contain
Python‑literal tuples like ('col1', 'col2') using ast.literal_eval instead of
json.loads, which fails for non‑JSON input.  Also handles single‑column lines
and skips malformed rows gracefully.
"""

import ast
import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from adaselect_pp.common import norm_name

logger = logging.getLogger(__name__)


class BenefitNormalizer:
    """Utility class for min‑max scaling + index‑creation cost lookup."""

    def __init__(self, alpha: float = 0.2):

        self.index_costs: Dict[Tuple[str, ...], float] = {}
        self.index_costs_by_key: Dict[Tuple[str, Tuple[str, ...]], float] = {}
        self.creation_cost_collisions: Dict[Tuple[str, ...], Set[str]] = {}
        self.creation_cost_unresolved: Set[Tuple[str, ...]] = set()
        self.creation_cost_path: str = ""
        self.creation_cost_status: str = "not_loaded"
        self.creation_cost_entries: int = 0
        self.creation_cost_raw_entries: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_creation_costs(self, benchmark: str, *, required: bool = False, db_con=None, vocabulary=None) -> None:
        """
        Read txt/{benchmark}_op_3_create_time.txt and normalize creation costs by column tuples.
        """
        fname = f"txt/{benchmark}_op_3_create_time.txt"
        path = Path(fname)
        self.creation_cost_path = str(path)
        if not path.exists():
            self.creation_cost_status = "missing"
            msg = f"Creation-cost file not found for benchmark={benchmark!r}: {path}"
            if required:
                raise FileNotFoundError(msg)
            logger.warning(msg)
            return

        raw: Dict[Tuple[str, ...], float] = {}
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    name_str, time_str = line.rsplit(maxsplit=1)
                    time_val = float(time_str)
                    # parse raw column tuple or single column
                    cols = tuple(ast.literal_eval(name_str)) if name_str.startswith(('(', '[')) else (name_str,)
                    cols = tuple(norm_name(c) for c in cols if norm_name(c))
                except Exception as e:
                    logger.debug("Skip bad line in %s: %s – %s", path, line, e)
                    continue

                # use raw column tuple as key
                if cols:
                    raw[cols] = time_val

        if not raw:
            self.creation_cost_status = "empty"
            msg = f"No creation-cost records parsed from {path}"
            if required:
                raise ValueError(msg)
            logger.warning(msg)
            return

        # Normalize via existing minmax_scale (keys are column tuples)
        normalized = self.minmax_scale(raw, len(raw))
        self.index_costs.update(normalized)
        self._load_table_aware_costs(normalized, db_con=db_con, vocabulary=vocabulary)
        self.creation_cost_raw_entries = len(raw)
        self.creation_cost_entries = len(normalized)
        self.creation_cost_status = "loaded"

        logger.info(
            "Creation costs loaded | benchmark=%s path=%s raw_entries=%d parsed_entries=%d status=%s",
            benchmark,
            path,
            self.creation_cost_raw_entries,
            self.creation_cost_entries,
            self.creation_cost_status,
        )
        if self.creation_cost_collisions:
            logger.warning(
                "Creation-cost tuple collisions detected | benchmark=%s count=%d sample=%s",
                benchmark,
                len(self.creation_cost_collisions),
                list(self.creation_cost_collisions.items())[:5],
            )
        if self.creation_cost_unresolved:
            logger.warning(
                "Creation-cost tuples unresolved to a table | benchmark=%s count=%d sample=%s",
                benchmark,
                len(self.creation_cost_unresolved),
                sorted(self.creation_cost_unresolved)[:5],
            )

    def _load_table_aware_costs(self, normalized: Dict[Tuple[str, ...], float], *, db_con=None, vocabulary=None) -> None:
        col_to_tables: Dict[str, Set[str]] = defaultdict(set)
        if vocabulary is not None and getattr(vocabulary, "enabled", False):
            for table, cols in getattr(vocabulary, "mapping", {}).items():
                for col in cols:
                    col_to_tables[norm_name(col)].add(norm_name(table))
        if db_con is not None:
            try:
                for table in db_con.get_tables():
                    t = norm_name(table)
                    for col in db_con.get_columns(table):
                        col_to_tables[norm_name(col)].add(t)
            except Exception as exc:
                logger.warning("Creation-cost table map unavailable from schema: %s", exc)

        if not col_to_tables:
            return

        for cols, cost in normalized.items():
            possible: Optional[Set[str]] = None
            for col in cols:
                tables = set(col_to_tables.get(norm_name(col), set()))
                possible = tables if possible is None else possible & tables
            possible = possible or set()
            if len(possible) == 1:
                table = next(iter(possible))
                self.index_costs_by_key[(table, tuple(cols))] = cost
            elif len(possible) > 1:
                self.creation_cost_collisions[tuple(cols)] = set(possible)
            else:
                self.creation_cost_unresolved.add(tuple(cols))

    def creation_cost_for(self, table: str, cols: Tuple[str, ...], default: float) -> float:
        key = (norm_name(table), tuple(norm_name(c) for c in cols if norm_name(c)))
        if key in self.index_costs_by_key:
            return float(self.index_costs_by_key[key])
        ctuple = key[1]
        if ctuple in self.creation_cost_collisions:
            return float(default)
        return float(self.index_costs.get(ctuple, default))


    @staticmethod
    def minmax_scale(values: Dict[Tuple[str, ...], float], size: int) -> Dict[Tuple[str, ...], float]:
        if not values:
            return {}
        vmin, vmax = min(values.values()), max(values.values())
        span = max(vmax - vmin, 1e-9)
        return {k: (v - vmin) / span for k, v in values.items()}
