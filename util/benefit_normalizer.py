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
from pathlib import Path
from typing import Dict, Set, Tuple

logger = logging.getLogger(__name__)


class BenefitNormalizer:
    """Utility class for min‑max scaling + index‑creation cost lookup."""

    def __init__(self, alpha: float = 0.2):

        self.index_costs: Dict[Tuple[str, ...], float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_creation_costs(self, benchmark: str) -> None:
        """
        Read txt/{benchmark}_op_3_create_time.txt and normalize creation costs by column tuples.
        """
        fname = f"txt/{benchmark}_op_3_create_time.txt"
        path = Path(fname)
        if not path.exists():
            logger.warning("Creation-cost file not found: %s", path)
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
                except Exception as e:
                    logger.debug("Skip bad line in %s: %s – %s", path, line, e)
                    continue

                # use raw column tuple as key
                raw[cols] = time_val

        if not raw:
            logger.warning("No creation-cost records parsed from %s", path)
            return

        # Normalize via existing minmax_scale (keys are column tuples)
        normalized = self.minmax_scale(raw, len(raw))
        self.index_costs.update(normalized)

        logger.info("Loaded %d creation-cost entries from %s", len(normalized), path)


    @staticmethod
    def minmax_scale(values: Dict[Tuple[str, ...], float], size: int) -> Dict[Tuple[str, ...], float]:
        if not values:
            return {}
        vmin, vmax = min(values.values()), max(values.values())
        span = max(vmax - vmin, 1e-9)
        return {k: (v - vmin) / span for k, v in values.items()}
